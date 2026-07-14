from __future__ import annotations

import curses
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from typing import Sequence

from .cluster import ClusterConfig, NodeReply, query_cluster_contexts


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
) -> list[str]:
    by_name = {reply.node.name: reply for reply in replies}
    selected = max(0, min(selected, len(config.nodes) - 1))
    lines = [
        _fit(
            f"GPUBK CLUSTER  {len(config.nodes)} nodes"
            + ("  refreshing..." if refreshing else ""),
            width,
        ),
        _fit("Node             State        GPUs Idle Mine  Actor", width),
    ]
    for index, node in enumerate(config.nodes):
        reply = by_name.get(node.name)
        marker = ">" if index == selected else " "
        if reply is None:
            state, gpus, idle, mine, actor = "waiting", "-", "-", "-", "-"
        elif reply.error:
            state, gpus, idle, mine, actor = "offline", "-", "-", "-", reply.error
        else:
            payload = reply.payload or {}
            advice = payload.get("gpu_advice", {}).get("gpus", [])
            policy = payload.get("policy", {})
            collector = policy.get("monitoring", {}).get("collector")
            state = collector.get("state", "ok") if isinstance(collector, dict) else "unknown"
            gpus = str(policy.get("gpu_count", len(advice)))
            idle = str(sum(1 for item in advice if item.get("live", {}).get("status") == "idle"))
            mine = str(sum(1 for item in payload.get("reservations", []) if item.get("mine")))
            identity = payload.get("actor", {})
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
    lines.append(_fit(f"{node.name}  {node.node_id}  priority={node.priority}", width))
    if reply is None:
        lines.append(_fit("Waiting for the first response.", width))
    elif reply.error:
        lines.append(_fit(f"Unavailable: {reply.error}", width))
    else:
        payload = reply.payload or {}
        lines.append(_fit("GPU  State       Util   Free VRAM   Predicted", width))
        for gpu in payload.get("gpu_advice", {}).get("gpus", []):
            live = gpu.get("live", {})
            memory = gpu.get("memory", {})
            history = gpu.get("history", {})
            util = live.get("utilization_percent")
            free = memory.get("free_mb")
            predicted = history.get("predicted_percent")
            lines.append(
                _fit(
                    f"{int(gpu.get('index', -1)):>3}  {str(live.get('status', '?')):<10} "
                    f"{_percent(util):>5}  {_memory(free):>10}   {_percent(predicted):>8}",
                    width,
                )
            )
        reservations = payload.get("reservations", [])
        if reservations:
            lines.append(_fit("", width))
            lines.append(_fit("ID        User             Mode       GPU      Start -> End", width))
            for reservation in reservations:
                short_id = reservation.get("short_id") or str(reservation.get("id", ""))[:8]
                lines.append(
                    _fit(
                        f"{short_id:<9} {str(reservation.get('username', '?')):<16} "
                        f"{str(reservation.get('mode', '?')):<10} "
                        f"{','.join(map(str, reservation.get('gpus', []))):<8} "
                        f"{reservation.get('start_at', '?')} -> {reservation.get('end_at', '?')}",
                        width,
                    )
                )
    footer = "Up/Down node  r refresh  q quit  |  bookings: bk c book ... or bk @NODE ..."
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
    selected = 0
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
                selected,
                max(1, width - 1),
                height,
                refreshing=pending is not None,
            )
            screen.erase()
            for row, line in enumerate(lines):
                if row >= height:
                    break
                try:
                    attribute = curses.A_NORMAL
                    if row == 0:
                        attribute |= curses.A_BOLD | (curses.color_pair(1) if colors else 0)
                    elif row == height - 1:
                        attribute |= curses.A_DIM
                    elif row == 2 + selected:
                        attribute |= curses.A_REVERSE
                    elif "offline" in line or "clock-skew" in line:
                        attribute |= curses.color_pair(2) if colors else curses.A_BOLD
                    elif "running" in line or " idle " in line:
                        attribute |= curses.color_pair(3) if colors else 0
                    screen.addnstr(row, 0, line, max(0, width - 1), attribute)
                except curses.error:
                    pass
            screen.refresh()
            key = screen.getch()
            if key in {ord("q"), 27}:
                break
            if key in {curses.KEY_UP, ord("k")}:
                selected = (selected - 1) % len(config.nodes)
            elif key in {curses.KEY_DOWN, ord("j")}:
                selected = (selected + 1) % len(config.nodes)
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


def _percent(value: object) -> str:
    return "-" if not isinstance(value, (int, float)) else f"{value:.0f}%"


def _memory(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"{value / 1024:.1f}GiB"
