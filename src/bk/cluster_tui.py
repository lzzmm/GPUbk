from __future__ import annotations

import curses
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from typing import Sequence

from .cluster import (
    MAX_CLOCK_SKEW_SECONDS,
    ClusterConfig,
    NodeReply,
    _clock_skew_seconds,
    _reservation_memory_text,
    _reservation_mode_text,
    _reservation_request_text,
    query_cluster_contexts,
)
from .cluster_transport import ClusterNode
from .timeparse import parse_iso


FOCUS_NODES = "nodes"
FOCUS_RESERVATIONS = "reservations"


@dataclass
class ClusterTuiState:
    selected_node: int = 0
    focus: str = FOCUS_NODES
    selected_reservation: int = -1


def run_cluster_tui(config: ClusterConfig) -> int:
    curses.wrapper(_cluster_tui_main, config)
    return 0


def render_cluster_lines(
    config: ClusterConfig,
    replies: Sequence[NodeReply],
    selected: int,
    width: int,
    height: int,
    *,
    refreshing: bool = False,
    focus: str = FOCUS_NODES,
    selected_reservation: int = -1,
) -> list[str]:
    by_name = {reply.node.name: reply for reply in replies}
    selected = max(0, min(selected, len(config.nodes) - 1))
    content_height = max(0, height - 1)
    node_rows = min(len(config.nodes), max(1, min(6, max(1, height // 3))))
    node_start = _window_start(len(config.nodes), node_rows, selected)
    node_end = min(len(config.nodes), node_start + node_rows)
    node_range = (
        f"  {node_start + 1}-{node_end}/{len(config.nodes)}"
        if node_start or node_end < len(config.nodes)
        else ""
    )
    lines = [
        _fit(
            f"GPUBK CLUSTER  {len(config.nodes)} nodes  focus={focus.upper()}"
            + ("  refreshing..." if refreshing else ""),
            width,
        ),
        _fit(f"Node             State        GPUs Idle Mine  Actor{node_range}", width),
    ]
    for index in range(node_start, node_end):
        node = config.nodes[index]
        reply = by_name.get(node.name)
        marker = ">" if focus == FOCUS_NODES and index == selected else " "
        if not node.enabled:
            state, gpus, idle, mine, actor = "disabled", "-", "-", "-", "maintenance"
        elif reply is None:
            state, gpus, idle, mine, actor = "waiting", "-", "-", "-", "-"
        elif reply.error:
            state, gpus, idle, mine, actor = "offline", "-", "-", "-", reply.error
        else:
            payload = reply.payload or {}
            advice = _dict_items(_mapping(payload.get("gpu_advice")).get("gpus"))
            policy = _mapping(payload.get("policy"))
            collector = _mapping(_mapping(policy.get("monitoring")).get("collector"))
            state = str(collector.get("state", "unknown"))
            skew = _clock_skew_seconds(payload)
            if skew is None or skew > MAX_CLOCK_SKEW_SECONDS:
                state = "clock-skew"
            gpus = str(policy.get("gpu_count", len(advice)))
            idle = str(
                sum(
                    1
                    for item in advice
                    if _mapping(item.get("live")).get("status") == "idle"
                )
            )
            mine = str(
                sum(
                    1
                    for item in _dict_items(payload.get("reservations"))
                    if item.get("mine")
                )
            )
            identity = _mapping(payload.get("actor"))
            actor = f"{identity.get('username', '?')}:{identity.get('uid', '?')}"
        lines.append(
            _fit(
                f"{marker}{node.name:<16} {state:<12} {gpus:>4} {idle:>4} {mine:>4}  {actor}",
                width,
            )
        )

    lines.append(_fit("", width))
    node = config.nodes[selected]
    reply = by_name.get(node.name)
    version = "-"
    if reply is not None and reply.error is None:
        software = _mapping((reply.payload or {}).get("software"))
        version = str(software.get("version", "legacy"))
    lines.append(
        _fit(
            f"{node.name}  {node.node_id}  v{version}  priority={node.priority}",
            width,
        )
    )
    if not node.enabled:
        lines.append(
            _fit(
                "Disabled by administrator; routing is paused while configuration and history are retained.",
                width,
            )
        )
    elif reply is None:
        lines.append(_fit("Waiting for the first response.", width))
    elif reply.error:
        lines.append(_fit(f"Unavailable: {reply.error}", width))
    else:
        payload = reply.payload or {}
        advice = _mapping(payload.get("gpu_advice"))
        gpus = _dict_items(advice.get("gpus"))
        reservations = _dict_items(payload.get("reservations"))
        reservation_rows = (
            min(len(reservations), max(2, height // 4)) if reservations else 0
        )
        reservation_space = reservation_rows + 2 if reservations else 0
        gpu_rows = min(
            len(gpus),
            max(0, content_height - len(lines) - reservation_space - 1),
        )
        if gpus and gpu_rows:
            gpu_suffix = (
                f"  showing {gpu_rows}/{len(gpus)}" if gpu_rows < len(gpus) else ""
            )
            lines.append(
                _fit(
                    f"GPU  State       Util   Free VRAM   Predicted{gpu_suffix}",
                    width,
                )
            )
        for gpu in gpus[:gpu_rows]:
            live = _mapping(gpu.get("live"))
            memory = _mapping(gpu.get("memory"))
            history = _mapping(gpu.get("history"))
            util = live.get("utilization_percent")
            free = memory.get("free_mb")
            predicted = history.get("predicted_percent")
            lines.append(
                _fit(
                    f"{str(gpu.get('index', '?')):>3}  {str(live.get('status', '?')):<10} "
                    f"{_percent(util):>5}  {_memory(free):>10}   {_percent(predicted):>8}",
                    width,
                )
            )
        if reservations and len(lines) + 2 < content_height:
            lines.append(_fit("", width))
            lines.append(
                _fit(
                    "  ID       User             Mode     Req     VRAM GPU      Start -> End",
                    width,
                )
            )
            visible_rows = max(0, content_height - len(lines))
            reservation_start = _window_start(
                len(reservations),
                visible_rows,
                selected_reservation if selected_reservation >= 0 else 0,
            )
            for index in range(
                reservation_start,
                min(len(reservations), reservation_start + visible_rows),
            ):
                reservation = reservations[index]
                short_id = (
                    reservation.get("short_id") or str(reservation.get("id", ""))[:8]
                )
                marker = (
                    ">"
                    if focus == FOCUS_RESERVATIONS and index == selected_reservation
                    else " "
                )
                lines.append(
                    _fit(
                        f"{marker} {short_id:<8} {str(reservation.get('username', '?')):<16} "
                        f"{_reservation_mode_text(reservation):<6} "
                        f"{_reservation_request_text(reservation):>5} "
                        f"{_reservation_memory_text(reservation):>8} "
                        f"{_gpu_text(reservation.get('gpus')):<8} "
                        f"{_local_time(reservation.get('start_at'))} -> "
                        f"{_local_time(reservation.get('end_at'))}",
                        width,
                    )
                )
    footer = (
        "RESERVATIONS  Up/Down select  Enter details  Tab nodes  b commands  r refresh  q quit"
        if focus == FOCUS_RESERVATIONS
        else "NODES  Up/Down select  Tab reservations  b commands  r refresh  q quit"
    )
    if len(lines) < height:
        lines.extend([""] * (height - len(lines) - 1))
    return lines[: max(0, height - 1)] + [_fit(footer, width)]


def _cluster_tui_main(screen, config: ClusterConfig) -> None:
    colors = False
    if curses.has_colors():
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_RED, -1)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
            curses.init_pair(4, curses.COLOR_YELLOW, -1)
            colors = True
        except curses.error:
            colors = False
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(100)
    state = ClusterTuiState()
    replies: list[NodeReply] = []
    stop_event = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    pending: Future | None = executor.submit(
        query_cluster_contexts,
        config,
        stop_event,
    )
    last_refresh = time.monotonic()
    try:
        while True:
            if pending is not None and pending.done():
                try:
                    replies = pending.result()
                except BaseException:
                    replies = []
                pending = None
                last_refresh = time.monotonic()
            if pending is None and time.monotonic() - last_refresh >= 5:
                pending = executor.submit(
                    query_cluster_contexts,
                    config,
                    stop_event,
                )
            height, width = screen.getmaxyx()
            lines = render_cluster_lines(
                config,
                replies,
                state.selected_node,
                max(1, width - 1),
                height,
                refreshing=pending is not None,
                focus=state.focus,
                selected_reservation=state.selected_reservation,
            )
            screen.erase()
            for row, line in enumerate(lines):
                if row >= height:
                    break
                try:
                    attribute = curses.A_NORMAL
                    if row == 0:
                        attribute |= curses.A_BOLD | (
                            curses.color_pair(1) if colors else 0
                        )
                    elif row == height - 1:
                        attribute |= curses.A_DIM
                    elif line.startswith(">"):
                        attribute |= curses.A_REVERSE
                    elif "offline" in line or "clock-skew" in line:
                        attribute |= curses.color_pair(2) if colors else curses.A_BOLD
                    elif "disabled" in line:
                        attribute |= curses.A_DIM
                    elif "running" in line or " idle " in line:
                        attribute |= curses.color_pair(3) if colors else 0
                    screen.addnstr(row, 0, line, max(0, width - 1), attribute)
                except curses.error:
                    pass
            screen.refresh()
            key = screen.getch()
            if key in {ord("q"), 27}:
                break
            reservations = _selected_reservations(config, replies, state.selected_node)
            if state.selected_reservation >= len(reservations):
                state.selected_reservation = len(reservations) - 1
            if key == 9 or key == getattr(curses, "KEY_BTAB", -1):
                state.focus = (
                    FOCUS_RESERVATIONS if state.focus == FOCUS_NODES else FOCUS_NODES
                )
                if state.focus == FOCUS_RESERVATIONS:
                    state.selected_reservation = -1
            elif key in {curses.KEY_UP, ord("k")}:
                if state.focus == FOCUS_NODES:
                    state.selected_node = (state.selected_node - 1) % len(config.nodes)
                    state.selected_reservation = -1
                else:
                    state.selected_reservation = max(-1, state.selected_reservation - 1)
            elif key in {curses.KEY_DOWN, ord("j")}:
                if state.focus == FOCUS_NODES:
                    state.selected_node = (state.selected_node + 1) % len(config.nodes)
                    state.selected_reservation = -1
                elif reservations:
                    state.selected_reservation = min(
                        len(reservations) - 1,
                        state.selected_reservation + 1,
                    )
            elif key in {curses.KEY_ENTER, 10, 13}:
                if (
                    state.focus == FOCUS_RESERVATIONS
                    and 0 <= state.selected_reservation < len(reservations)
                ):
                    _show_dialog(
                        screen,
                        "Reservation details",
                        _reservation_detail_lines(
                            config.nodes[state.selected_node],
                            reservations[state.selected_reservation],
                        ),
                    )
            elif key in {ord("b"), ord("?")}:
                _show_dialog(
                    screen,
                    "Cluster commands",
                    _cluster_command_lines(config.nodes[state.selected_node]),
                )
            elif key == ord("r") and pending is None:
                pending = executor.submit(
                    query_cluster_contexts,
                    config,
                    stop_event,
                )
    finally:
        stop_event.set()
        executor.shutdown(wait=True, cancel_futures=True)


def _fit(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: max(0, width - 1)] + "~"


def _window_start(total: int, rows: int, selected: int) -> int:
    if total <= 0 or rows <= 0 or total <= rows:
        return 0
    anchor = min(max(0, selected), total - 1)
    return min(total - rows, max(0, anchor - rows + 1))


def _selected_reservations(
    config: ClusterConfig,
    replies: Sequence[NodeReply],
    selected_node: int,
) -> list[dict]:
    node = config.nodes[min(max(0, selected_node), len(config.nodes) - 1)]
    reply = next((item for item in replies if item.node.name == node.name), None)
    if reply is None or reply.error is not None:
        return []
    return _dict_items((reply.payload or {}).get("reservations"))


def _reservation_detail_lines(
    node: ClusterNode,
    reservation: dict,
) -> list[str]:
    short_id = reservation.get("short_id") or str(reservation.get("id", ""))[:8]
    qualified = f"{node.name}/{short_id}"
    owner = f"{reservation.get('username', '?')} (UID {reservation.get('uid', '?')})"
    access = (
        "you can edit or cancel" if reservation.get("mine") is True else "read-only"
    )
    lines = [
        f"ID: {qualified}",
        f"Full UUID: {reservation.get('id', '?')}",
        f"Owner: {owner} - {access}",
        f"Mode: {_reservation_mode_text(reservation)}",
        f"Capacity request: {_reservation_request_text(reservation)}",
        f"Expected VRAM/GPU: {_reservation_memory_text(reservation)}",
        f"GPUs: {_gpu_text(reservation.get('gpus'))}",
        f"Start: {_detail_time(reservation.get('start_at'))}",
        f"End: {_detail_time(reservation.get('end_at'))}",
    ]
    job = reservation.get("job")
    if isinstance(job, dict):
        lines.extend(
            (
                f"Job: {job.get('status', 'unknown')}",
                f"Command: {job.get('summary', 'private command')}",
            )
        )
    lines.append("")
    if reservation.get("mine") is True:
        lines.extend(
            (
                f"Edit:   bk c e {qualified} -d 1h",
                f"Cancel: bk c d {qualified}",
            )
        )
    else:
        lines.append(
            "This reservation is visible for planning, but only its owner can change it."
        )
    return lines


def _cluster_command_lines(node: ClusterNode) -> list[str]:
    return [
        "Book the earliest node:",
        "  bk c 1 30m",
        "  bk c x 1 1h",
        "",
        f"Book this node ({node.name}):",
        f"  bk @{node.name} 1 30m",
        "",
        "Compare without writing:",
        "  bk c rec 2 1h",
        "",
        "Readiness and usage:",
        "  bk c check",
        "  bk c u -s 7d",
    ]


def _show_dialog(screen, title: str, lines: Sequence[str]) -> None:
    height, width = screen.getmaxyx()
    if height < 7 or width < 32:
        return
    win_height = height - 2
    win_width = width - 2
    win = curses.newwin(win_height, win_width, 1, 1)
    win.keypad(True)
    page_rows = max(1, win_height - 3)
    offset = 0
    while True:
        win.erase()
        win.box()
        try:
            win.addnstr(0, 2, f" {title} ", max(0, win_width - 4), curses.A_BOLD)
            for row, line in enumerate(lines[offset : offset + page_rows], 1):
                win.addnstr(row, 2, line, max(0, win_width - 4))
            footer = "Up/Down scroll  q/Esc/Enter close"
            win.addnstr(
                win_height - 2,
                2,
                footer,
                max(0, win_width - 4),
                curses.A_DIM,
            )
        except curses.error:
            pass
        win.refresh()
        key = win.getch()
        maximum = max(0, len(lines) - page_rows)
        if key in {ord("q"), 27, curses.KEY_ENTER, 10, 13}:
            return
        if key in {curses.KEY_UP, ord("k")}:
            offset = max(0, offset - 1)
        elif key in {curses.KEY_DOWN, ord("j")}:
            offset = min(maximum, offset + 1)


def _detail_time(value: object) -> str:
    if not isinstance(value, str):
        return "?"
    try:
        return parse_iso(value).astimezone().strftime("%a %Y-%m-%d %H:%M %z")
    except (TypeError, ValueError):
        return value


def _percent(value: object) -> str:
    return (
        "-"
        if isinstance(value, bool) or not isinstance(value, (int, float))
        else f"{value:.0f}%"
    )


def _memory(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    return f"{value / 1024:.1f}GiB"


def _local_time(value: object) -> str:
    if not isinstance(value, str):
        return "?"
    try:
        return parse_iso(value).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return value


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _dict_items(value: object) -> list[dict]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _gpu_text(value: object) -> str:
    if not isinstance(value, list):
        return "-"
    indices = [
        item
        for item in value
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0
    ]
    return ",".join(map(str, indices)) if indices else "-"
