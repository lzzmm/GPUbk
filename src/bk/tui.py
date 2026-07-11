from __future__ import annotations

import curses
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

from .advisor import build_gpu_advice
from .allocator import AllocatorDecision, apply_external_allocator
from .config import Config
from .gpu import GpuSnapshot, snapshot
from .identity import current_actor
from .models import (
    MODE_EXCLUSIVE,
    MODE_SHARED,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    Actor,
    BookingError,
    BookingRequest,
    EditRequest,
)
from .schedule_index import ReservationIndex
from .scheduler import (
    add_booking,
    availability_detail,
    cancel_booking,
    edit_booking,
    find_earliest_slot,
    list_active,
    max_shared_record_count_for_reservation,
)
from .storage import LedgerStore
from .timeparse import format_local_range, parse_iso, parse_memory_mb, utc_now
from .usage import ProcessUsage, classify_process_usage, summarize_process_command


COLOR_HEADER = 1
COLOR_FREE = 2
COLOR_SHARED = 3
COLOR_MINE = 4
COLOR_EXCLUSIVE = 5
COLOR_SELECTED = 6
COLOR_ERROR = 7
COLOR_MUTED = 8
COLOR_PREVIEW_SHARED = 9
COLOR_PREVIEW_EXCLUSIVE = 10
COLOR_RES_BASE = 11

ADD_STEP_MINUTES = 5
BAR_CHAR = "█"
SHARED_CHAR = "▓"
SPLIT_CHAR = "▀"
WEAVE_CHARS = ("▚", "▞")
FREE_CHAR = "."
NOW_CHAR = "│"
NOW_CONTEXT_COLUMNS = 6
ZOOM_LEVELS = [5, 10, 15, 30, 60]
DEFAULT_ZOOM_INDEX = 0
FOCUS_RESERVATIONS = "reservations"
FOCUS_GPUS = "gpus"
WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
RESERVATION_COLORS = [
    curses.COLOR_CYAN,
    curses.COLOR_YELLOW,
    curses.COLOR_GREEN,
    curses.COLOR_MAGENTA,
    curses.COLOR_BLUE,
    curses.COLOR_WHITE,
    curses.COLOR_RED,
    curses.COLOR_CYAN,
]
DARK_RESERVATION_COLORS_256 = [75, 173, 78, 176, 80, 186, 167, 117]
LIGHT_RESERVATION_COLORS_256 = [25, 130, 28, 90, 31, 94, 124, 23]
MIXED_COLOR_PAIRS: dict[Tuple[int, int], int] = {}
ACTIVE_TUI_THEME = "dark"

HELP_PAGES: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...] = (
    (
        "Navigate",
        (
            ("", "TIMELINE"),
            ("Left / Right", "Pan one hour into history or the future"),
            ("n", "Jump back to the live NOW window"),
            ("+ / -", "Zoom the timeline from 5 to 60 minutes per column"),
            ("r", "Refresh now; auto-refresh already runs every second"),
            ("c", "Toggle the dark or light color theme"),
            ("", "FOCUS AND ACTIONS"),
            ("Up / Down", "Move through reservations or GPU rows"),
            ("Tab", "Switch between reservation and GPU focus"),
            ("a / e / d", "Add, edit, or delete a reservation"),
            ("?", "Open this help"),
            ("q / Esc", "Quit GPUbk"),
        ),
    ),
    (
        "Add / Edit",
        (
            ("1-9", "Set GPU count; find earliest legal slot on best GPUs"),
            ("f", "Find earliest slot on any GPUs; keep selected count"),
            ("g", "Find earliest slot on exactly the selected GPUs"),
            ("Left / Right", "Move start time by 5 minutes"),
            ("Up / Down", "Move the GPU cursor"),
            ("Space", "Select or clear the current GPU"),
            ("+ / -  [ / ]", "Shorten or extend duration by 5 minutes"),
            ("s / x", "Choose shared or exclusive mode"),
            ("m", "Set expected VRAM per GPU, such as 12g"),
            ("r", "Reset Add defaults or restore original Edit values"),
            ("Enter / Esc", "Submit the exact preview or cancel"),
            ("", "EDIT IS LIMITED TO RESERVATIONS THAT HAVE NOT STARTED"),
        ),
    ),
    (
        "Timeline",
        (
            ("NOW", "Bright marker shows the current wall-clock time"),
            ("History", "Dimmed cells left of NOW show reservation history"),
            ("Date", "Date and weekday are repeated at every midnight"),
            ("Colors", "Each booking keeps one timeline and table color"),
            ("Shared", "Split or woven cells divide space among sharers"),
            ("GPU focus", "Tab to expand share lanes and live processes"),
            ("Reservation", "Select a row to blink its exact interval"),
            ("Util history", "Run: bk usage --rollups"),
            ("Live context", "Run: bk agent context --compact"),
            ("Theme", "Auto-detect; set BK_TUI_THEME=dark or light"),
            ("", "PAST RESERVATIONS ARE READ-ONLY"),
        ),
    ),
    (
        "Quick Tour",
        (
            ("bk 2 1h", "Book two shared GPUs at best available time"),
            ("bk 1 30m --mem 12g", "Book shared capacity with expected VRAM"),
            ("bk x 1 30m", "Book one GPU exclusively; x means exclusive"),
            ("bk tui", "Open this timeline"),
            ("a then 2", "Preview earliest two-GPU slot; Enter confirms"),
            ("Tab", "Inspect a GPU's sharers and processes"),
            ("e", "Edit a selected future reservation on timeline"),
            ("bk usage --rollups", "Inspect historical utilization summaries"),
            ("bk agent context", "Give an Agent privacy-safe allocation context"),
            ("bk doctor", "Read-only policy and ledger diagnostics"),
        ),
    ),
)


@dataclass
class TuiState:
    offset_slots: int = 0
    zoom_index: int = DEFAULT_ZOOM_INDEX
    selected: int = 0
    focus: str = FOCUS_RESERVATIONS
    selected_gpu: int = 0
    message: str = ""
    error: bool = False
    add_mode: bool = False
    edit_mode: bool = False
    edit_reservation_id: Optional[str] = None
    editor_view_start: Optional[datetime] = None
    add_cursor_gpu: int = 0
    add_start_steps: int = 0
    add_duration_steps: int = 6
    add_selected_gpus: set[int] = field(default_factory=set)
    add_booking_mode: str = MODE_SHARED
    add_expected_memory_mb: Optional[int] = None
    gpu_memory_capacity_mb: dict[int, int] = field(default_factory=dict)
    gpu_memory_free_mb: dict[int, int] = field(default_factory=dict)

    @property
    def slot_minutes(self) -> int:
        return ZOOM_LEVELS[self.zoom_index]

    @property
    def editor_active(self) -> bool:
        return self.add_mode or self.edit_mode


@dataclass(frozen=True)
class AddPreview:
    start: datetime
    end: datetime
    selected_gpus: Tuple[int, ...]
    cursor_gpu: int
    mode: str
    valid: bool
    reason: str = ""
    blink: bool = False


def run_tui(config: Config, store: LedgerStore) -> int:
    try:
        return curses.wrapper(_run, config, store)
    except curses.error:
        _print_fallback(config, store)
        return 0


def _run(stdscr, config: Config, store: LedgerStore) -> int:
    _init_curses(stdscr)
    state = TuiState()
    while True:
        try:
            _draw(stdscr, config, store, state)
            key = stdscr.getch()
            if not state.editor_active and key in (ord("q"), ord("Q"), 27):
                return 0
            _handle_key(stdscr, key, config, store, state)
        except Exception as exc:
            state.message = str(exc)
            state.error = True


def _init_curses(stdscr) -> None:
    curses.curs_set(0)
    stdscr.timeout(1000)
    stdscr.keypad(True)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
    _apply_tui_theme(_resolve_tui_theme())


def _resolve_tui_theme(theme: Optional[str] = None, colorfgbg: Optional[str] = None) -> str:
    configured = (theme if theme is not None else os.environ.get("BK_TUI_THEME", "auto")).strip().lower()
    if configured in {"dark", "light"}:
        return configured
    terminal_colors = colorfgbg if colorfgbg is not None else os.environ.get("COLORFGBG", "")
    try:
        background = int(terminal_colors.split(";")[-1])
    except (TypeError, ValueError):
        return "dark"
    return "light" if background in {7, 15} or background >= 250 else "dark"


def _apply_tui_theme(theme: str) -> None:
    global ACTIVE_TUI_THEME
    ACTIVE_TUI_THEME = "light" if theme == "light" else "dark"
    MIXED_COLOR_PAIRS.clear()
    try:
        has_colors = curses.has_colors()
    except curses.error:
        return
    if not has_colors:
        return

    extended = getattr(curses, "COLORS", 0) >= 256
    for pair_id, foreground, background in _theme_color_pairs(ACTIVE_TUI_THEME, extended):
        curses.init_pair(pair_id, foreground, background)
    palette = _reservation_palette(ACTIVE_TUI_THEME)
    for offset, color in enumerate(palette):
        curses.init_pair(COLOR_RES_BASE + offset, color, -1)
    _init_mixed_color_pairs(palette)


def _theme_color_pairs(theme: str, extended: bool) -> Tuple[Tuple[int, int, int], ...]:
    if extended:
        if theme == "light":
            colors = (24, 28, 124, 25, 124, 30, 130)
        else:
            colors = (73, 108, 167, 117, 203, 80, 172)
        shared, mine, exclusive, selected, error, preview_shared, preview_exclusive = colors
        return (
            (COLOR_HEADER, 255, 24),
            (COLOR_FREE, -1, -1),
            (COLOR_SHARED, shared, -1),
            (COLOR_MINE, mine, -1),
            (COLOR_EXCLUSIVE, exclusive, -1),
            (COLOR_SELECTED, selected, -1),
            (COLOR_ERROR, error, -1),
            (COLOR_MUTED, -1, -1),
            (COLOR_PREVIEW_SHARED, preview_shared, -1),
            (COLOR_PREVIEW_EXCLUSIVE, preview_exclusive, -1),
        )
    selected = curses.COLOR_BLUE if theme == "light" else curses.COLOR_CYAN
    return (
        (COLOR_HEADER, curses.COLOR_WHITE, curses.COLOR_BLUE),
        (COLOR_FREE, -1, -1),
        (COLOR_SHARED, curses.COLOR_CYAN, -1),
        (COLOR_MINE, curses.COLOR_GREEN, -1),
        (COLOR_EXCLUSIVE, curses.COLOR_RED, -1),
        (COLOR_SELECTED, selected, -1),
        (COLOR_ERROR, curses.COLOR_RED, -1),
        (COLOR_MUTED, -1, -1),
        (COLOR_PREVIEW_SHARED, curses.COLOR_CYAN, -1),
        (COLOR_PREVIEW_EXCLUSIVE, curses.COLOR_MAGENTA, -1),
    )


def _handle_key(stdscr, key: int, config: Config, store: LedgerStore, state: TuiState) -> None:
    if state.editor_active:
        _handle_add_key(key, config, store, state, stdscr=stdscr)
        return
    if key in (ord("r"), ord("R")):
        state.message = "refreshed now (automatic refresh: 1s)"
        state.error = False
        return
    if key in (ord("c"), ord("C")):
        next_theme = "light" if ACTIVE_TUI_THEME == "dark" else "dark"
        _apply_tui_theme(next_theme)
        state.message = f"theme: {ACTIVE_TUI_THEME} (set BK_TUI_THEME to make it persistent)"
        state.error = False
        return
    if key in (curses.KEY_RIGHT, ord("l"), ord("L")):
        _pan_timeline(config, state, 1)
        return
    if key in (curses.KEY_LEFT, ord("h"), ord("H")):
        _pan_timeline(config, state, -1)
        return
    if key in (ord("n"), ord("N"), curses.KEY_HOME):
        state.offset_slots = 0
        state.message = "live NOW window"
        state.error = False
        return
    if key in (ord("+"), ord("=")):
        _change_zoom(state, -1)
        state.message = f"zoom {state.slot_minutes}m/col"
        state.error = False
        return
    if key in (ord("-"), ord("_")):
        _change_zoom(state, 1)
        state.message = f"zoom {state.slot_minutes}m/col"
        state.error = False
        return
    if key == 9:
        _toggle_focus(config, store, state)
        return
    if key in (curses.KEY_DOWN, ord("j"), ord("J")):
        _move_focus_down(config, store, state)
        return
    if key in (curses.KEY_UP, ord("k"), ord("K")):
        _move_focus_up(config, store, state)
        return
    if key in (ord("a"), ord("A")):
        if state.focus == FOCUS_GPUS:
            state.add_cursor_gpu = state.selected_gpu
        _start_add_select(config, state)
        return
    if key in (ord("e"), ord("E")):
        if state.focus != FOCUS_RESERVATIONS:
            state.message = "switch to reservations to edit"
            state.error = True
            return
        _start_edit_select(config, store, state)
        return
    if key in (ord("d"), ord("D")):
        if state.focus != FOCUS_RESERVATIONS:
            state.message = "switch to reservations to delete"
            state.error = True
            return
        _delete_selected(stdscr, store, state)
        return
    if key in (ord("?"), ord("p"), ord("P")):
        if stdscr is None:
            state.message = "help: navigation, add/edit, timeline, and quick tour"
            state.error = False
        else:
            _help_dialog(stdscr)


def _pan_timeline(config: Config, state: TuiState, direction: int) -> None:
    step_slots = max(1, 60 // state.slot_minutes)
    retention_slots = max(0, config.ledger_retention_days * 24 * 60 // state.slot_minutes)
    earliest_offset = -max(0, retention_slots - NOW_CONTEXT_COLUMNS)
    latest_offset = max(0, config.queue_search_hours * 60 // state.slot_minutes)
    requested = state.offset_slots + (step_slots if direction > 0 else -step_slots)
    state.offset_slots = min(latest_offset, max(earliest_offset, requested))
    if state.offset_slots == earliest_offset and requested < earliest_offset:
        state.message = f"history limit: {config.ledger_retention_days}d"
    elif state.offset_slots == latest_offset and requested > latest_offset:
        state.message = f"future search limit: {config.queue_search_hours}h"
    else:
        direction_label = "future" if direction > 0 else "history"
        state.message = f"timeline: {direction_label}; n returns to NOW"
    state.error = False


def _change_zoom(state: TuiState, direction: int) -> None:
    offset_minutes = state.offset_slots * state.slot_minutes
    state.zoom_index = min(len(ZOOM_LEVELS) - 1, max(0, state.zoom_index + direction))
    state.offset_slots = int(round(offset_minutes / state.slot_minutes))


def _toggle_focus(config: Config, store: LedgerStore, state: TuiState) -> None:
    if state.focus == FOCUS_GPUS:
        state.focus = FOCUS_RESERVATIONS
        state.message = "reservation focus"
    else:
        mine = _own_reservations(store)
        if mine:
            state.selected = min(max(0, state.selected), len(mine) - 1)
            gpus = [int(gpu) for gpu in mine[state.selected].get("gpus", [])]
            if gpus:
                state.selected_gpu = min(max(gpus[0], 0), config.gpu_count - 1)
        state.focus = FOCUS_GPUS
        state.selected_gpu = min(max(state.selected_gpu, 0), config.gpu_count - 1)
        state.message = f"GPU {state.selected_gpu} focus"
    state.error = False


def _move_focus_up(config: Config, store: LedgerStore, state: TuiState) -> None:
    if state.focus == FOCUS_GPUS:
        state.selected_gpu = max(0, state.selected_gpu - 1)
        state.message = f"GPU {state.selected_gpu} focus"
        state.error = False
        return
    if state.selected > 0:
        state.selected -= 1
        return
    state.focus = FOCUS_GPUS
    state.selected_gpu = max(0, config.gpu_count - 1)
    state.message = f"GPU {state.selected_gpu} focus"
    state.error = False


def _move_focus_down(config: Config, store: LedgerStore, state: TuiState) -> None:
    if state.focus == FOCUS_GPUS:
        if state.selected_gpu < config.gpu_count - 1:
            state.selected_gpu += 1
            state.message = f"GPU {state.selected_gpu} focus"
        else:
            state.focus = FOCUS_RESERVATIONS
            state.message = "reservation focus"
        state.error = False
        return
    mine = _own_reservations(store)
    state.selected = min(max(0, len(mine) - 1), state.selected + 1)


def _draw(stdscr, config: Config, store: LedgerStore, state: TuiState) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 18 or width < 72:
        _addstr(stdscr, 0, 0, "Terminal too small. Need at least 72x18.", width, COLOR_ERROR)
        stdscr.refresh()
        return

    now = utc_now()
    ledger = store.load()
    active_index = ReservationIndex.from_ledger(ledger, now)
    active = active_index.records()
    state.selected_gpu = min(max(state.selected_gpu, 0), config.gpu_count - 1)
    focused_gpu = state.selected_gpu if state.focus == FOCUS_GPUS and not state.editor_active else None
    selected_id = _timeline_selected_id(active, state)
    gpu_snapshots = _normalized_snapshots(config)
    state.gpu_memory_capacity_mb = {
        gpu.index: gpu.memory_total_mb for gpu in gpu_snapshots if gpu.memory_total_mb > 0
    }
    state.gpu_memory_free_mb = {
        gpu.index: max(0, gpu.memory_total_mb - gpu.memory_used_mb)
        for gpu in gpu_snapshots
        if gpu.memory_total_mb > 0
    }
    usage_by_gpu = classify_process_usage(gpu_snapshots, active, now)
    gpu_by_index = {gpu.index: gpu for gpu in gpu_snapshots}

    timeline_top = 3
    label_width = min(40, max(30, width // 3))
    timeline_width = max(24, width - label_width - 2)
    default_view_start = _timeline_view_start(now, state)
    view_start = state.editor_view_start if state.editor_active and state.editor_view_start else default_view_start
    view_end = view_start + timedelta(minutes=timeline_width * state.slot_minutes)
    timeline_index = ReservationIndex.from_ledger(
        ledger,
        min(now, view_start),
        statuses=(STATUS_ACTIVE, STATUS_EXPIRED),
    )
    timeline_records = timeline_index.records()
    preview = _build_add_preview(ledger, config, state, view_start) if state.editor_active else None
    color_map = _reservation_color_map(timeline_records, timeline_index)

    _draw_header(stdscr, config, now, view_start, view_end, width, state)
    _draw_editor_banner(stdscr, 2, width, state, preview)
    _draw_time_axis(stdscr, timeline_top, label_width, timeline_width, view_start, view_end, width, now)
    row = timeline_top + 4
    for gpu in gpu_snapshots:
        if row + 1 > height - 8:
            break
        _draw_gpu_row(
            stdscr,
            row,
            label_width,
            timeline_width,
            gpu,
            color_map,
            timeline_records,
            view_start,
            state.slot_minutes,
            selected_id,
            width,
            preview,
            1,
            config.max_shared_users,
            focused_gpu == gpu.index,
            usage_by_gpu.get(gpu.index, []),
            timeline_index,
            now,
        )
        row += 1

    row = _draw_selected_gpu_lanes(
        stdscr,
        row,
        label_width,
        timeline_width,
        timeline_records,
        view_start,
        state.slot_minutes,
        selected_id,
        width,
        color_map,
        config.max_shared_users,
        height,
        focused_gpu,
        timeline_index,
        now,
    )

    if focused_gpu is not None:
        gpu = gpu_by_index.get(focused_gpu, GpuSnapshot(index=focused_gpu, name="unknown"))
        _draw_process_panel(stdscr, row, width, height, gpu, usage_by_gpu.get(focused_gpu, []))
    else:
        panel_top = min(height - 7, row + 1)
        _draw_reservation_panel(
            stdscr,
            panel_top,
            width,
            height,
            store,
            active,
            state,
            selected_id,
            config.max_shared_users,
            color_map,
        )
    _draw_footer(stdscr, height, width, state, preview)
    stdscr.refresh()


def _draw_header(
    stdscr,
    config: Config,
    now: datetime,
    view_start: datetime,
    view_end: datetime,
    width: int,
    state: TuiState,
) -> None:
    title, details = _header_lines(config, now, view_start, view_end, width, state)
    _addstr(stdscr, 0, 0, title.ljust(width), width, COLOR_HEADER)
    _addstr(stdscr, 1, 0, details.ljust(width), width, COLOR_MUTED)


def _header_lines(
    config: Config,
    now: datetime,
    view_start: datetime,
    view_end: datetime,
    width: int,
    state: TuiState,
) -> Tuple[str, str]:
    local_now = now.astimezone()
    local_start = view_start.astimezone()
    local_end = view_end.astimezone()
    if view_end <= now:
        window_mode = "HISTORY"
    elif view_start > now:
        window_mode = "FUTURE"
    else:
        window_mode = "LIVE"
    wide_title = (
        f" GPUbk | {window_mode} | now {local_now:%Y-%m-%d %H:%M:%S %z} "
        f"| window {local_start:%m-%d %H:%M} -> {local_end:%m-%d %H:%M} "
        f"| {state.slot_minutes}m/col "
    )
    wide_details = (
        f" data={config.data_dir} | shared_limit={config.max_shared_users} "
        "| refresh=1s | n NOW | q quit | ? help"
    )
    if width >= 100 and len(wide_title) < width and len(wide_details) < width:
        title = wide_title
        details = wide_details
    else:
        title = (
            f" GPUbk {window_mode} | {_weekday_label(local_now)} {local_now:%m-%d %H:%M:%S} "
            f"| {local_start:%m-%d %H:%M}->{local_end:%m-%d %H:%M} | {state.slot_minutes}m"
        )
        suffix = f" | share={config.max_shared_users} | 1s refresh | n NOW | ? help"
        path_budget = max(1, width - len(" data=") - len(suffix) - 1)
        details = f" data={_truncate(config.data_dir.name or str(config.data_dir), path_budget)}{suffix}"
    limit = max(0, width - 1)
    return title[:limit], details[:limit]


def _draw_time_axis(
    stdscr,
    row: int,
    label_width: int,
    timeline_width: int,
    start: datetime,
    end: datetime,
    width: int,
    now: Optional[datetime] = None,
) -> None:
    dates, hours, minutes, ruler = _time_axis_lines(start, end, timeline_width)
    local_start = start.astimezone()
    _addstr(stdscr, row, 0, f"Date {_date_label(local_start)}".ljust(label_width), width, COLOR_MUTED)
    _addstr(stdscr, row + 1, 0, "Hour".ljust(label_width), width, COLOR_MUTED)
    _addstr(stdscr, row + 2, 0, "Minute".ljust(label_width), width, COLOR_MUTED)
    _addstr(stdscr, row + 3, 0, "".ljust(label_width), width, COLOR_MUTED)
    _addstr(stdscr, row, label_width, dates, width, COLOR_MUTED)
    _addstr(stdscr, row + 1, label_width, hours, width, COLOR_MUTED)
    _addstr(stdscr, row + 2, label_width, minutes, width, COLOR_MUTED)
    _addstr(stdscr, row + 3, label_width, ruler, width, COLOR_MUTED)
    now_col = _timeline_now_col(now, start, end, timeline_width)
    if now_col is not None:
        label_col = min(max(0, now_col - 1), max(0, timeline_width - 3))
        _addstr(stdscr, row + 2, label_width + label_col, "NOW", width, COLOR_SELECTED, curses.A_BOLD)
        _addstr(stdscr, row + 3, label_width + now_col, NOW_CHAR, width, COLOR_SELECTED, curses.A_BOLD)


def _draw_editor_banner(
    stdscr,
    row: int,
    width: int,
    state: TuiState,
    preview: Optional[AddPreview],
) -> None:
    if not state.editor_active or preview is None:
        return
    color = _preview_color(preview.mode) if preview.valid else COLOR_ERROR
    _addstr(stdscr, row, 0, _editor_banner_text(state, preview), width, color, curses.A_BOLD)


def _editor_banner_text(state: TuiState, preview: AddPreview) -> str:
    operation = "EDIT" if state.edit_mode else "ADD"
    if state.edit_mode and state.edit_reservation_id:
        operation += f" {state.edit_reservation_id[:8]}"
    mode = "S" if preview.mode == MODE_SHARED else "X"
    gpu_text = ",".join(map(str, preview.selected_gpus)) or "-"
    local_start = preview.start.astimezone()
    local_end = preview.end.astimezone()
    status = "READY" if preview.valid else "BLOCKED"
    memory = _editor_memory_label(state)
    return (
        f" {operation} {mode} | {len(preview.selected_gpus)} GPU [{gpu_text}] | "
        f"{_weekday_label(local_start)} {local_start:%m-%d %H:%M}->{local_end:%H:%M} | "
        f"{_duration_text(local_end - local_start)} | {memory} | {status} "
    )


def _draw_gpu_row(
    stdscr,
    row: int,
    label_width: int,
    timeline_width: int,
    gpu: GpuSnapshot,
    color_map: dict[str, int],
    active: Sequence[dict],
    start: datetime,
    slot_minutes: int,
    selected_id: Optional[str],
    width: int,
    preview: Optional[AddPreview],
    band_rows: int,
    shared_limit: int,
    focused: bool = False,
    process_usage: Sequence[ProcessUsage] = (),
    reservation_index: Optional[ReservationIndex] = None,
    now: Optional[datetime] = None,
) -> None:
    view_end = start + timedelta(minutes=slot_minutes * timeline_width)
    peak_shared = _peak_shared_count_for_gpu(active, gpu.index, start, view_end, reservation_index)
    violations = sum(1 for item in process_usage if item.violation)
    label = _gpu_label(gpu, label_width, peak_shared, shared_limit, violations)
    cursor_active = preview is not None and gpu.index == preview.cursor_gpu
    if cursor_active:
        label_color = _preview_color(preview.mode)
    elif violations:
        label_color = COLOR_ERROR
    else:
        label_color = COLOR_SELECTED if focused else COLOR_MUTED
    label_attr = curses.A_BOLD if cursor_active or focused else 0
    for lane in range(max(1, band_rows)):
        row_label = label if lane == 0 else ""
        if focused and lane == 0:
            row_label = ">" + row_label[: max(0, label_width - 1)]
        _addstr(stdscr, row + lane, 0, row_label[:label_width].ljust(label_width), width, label_color, label_attr)
        for col in range(timeline_width):
            left = start + timedelta(minutes=slot_minutes * col)
            right = left + timedelta(minutes=slot_minutes)
            preview_cell = _preview_cell_for_gpu(gpu.index, left, right, preview, lane, band_rows)
            if preview_cell is not None:
                char, color, attr = preview_cell
            else:
                char, color, attr = _cell_for_gpu(
                    gpu.index,
                    color_map,
                    active,
                    left,
                    right,
                    selected_id,
                    col,
                    lane,
                    band_rows,
                    reservation_index,
                )
            char, color, attr = _decorate_timeline_cell(char, color, attr, left, right, now)
            _addstr(stdscr, row + lane, label_width + col, char, width, color, attr)


def _draw_selected_gpu_lanes(
    stdscr,
    row: int,
    label_width: int,
    timeline_width: int,
    active: Sequence[dict],
    start: datetime,
    slot_minutes: int,
    selected_id: Optional[str],
    width: int,
    color_map: dict[str, int],
    shared_limit: int,
    height: int,
    focused_gpu: Optional[int] = None,
    reservation_index: Optional[ReservationIndex] = None,
    now: Optional[datetime] = None,
) -> int:
    view_end = start + timedelta(minutes=slot_minutes * timeline_width)
    if focused_gpu is not None:
        gpu = focused_gpu
        related = _visible_shared_reservations(active, gpu, start, view_end, reservation_index)
        minimum_lanes = 1
        reserved_rows = 6
    else:
        selected = _reservation_by_id(active, selected_id)
        if selected is None or selected.get("mode") != MODE_SHARED:
            return row
        detail = _selected_share_detail(active, selected, start, view_end)
        if detail is None:
            return row
        gpu, related = detail
        minimum_lanes = 2
        reserved_rows = 8
    lanes = _share_detail_rows(height, row, len(related), shared_limit, reserved_rows)
    if lanes < minimum_lanes:
        return row

    visible = related[:lanes]
    for lane, reservation in enumerate(visible):
        hidden = len(related) - lanes if lane == lanes - 1 else 0
        label = _share_lane_label(gpu, lane, len(related), reservation, hidden)
        label_color = _reservation_color(reservation, color_map)
        reservation_start = parse_iso(reservation["start_at"])
        reservation_end = parse_iso(reservation["end_at"])
        _addstr(stdscr, row + lane, 0, label[:label_width].ljust(label_width), width, label_color)
        for col in range(timeline_width):
            left = start + timedelta(minutes=slot_minutes * col)
            right = left + timedelta(minutes=slot_minutes)
            if reservation_start < right and left < reservation_end:
                char = BAR_CHAR
                color = label_color
                attr = _selected_bar_attr() if reservation.get("id") == selected_id else curses.A_BOLD
            else:
                char, color, attr = FREE_CHAR, COLOR_FREE, 0
            char, color, attr = _decorate_timeline_cell(char, color, attr, left, right, now)
            _addstr(stdscr, row + lane, label_width + col, char, width, color, attr)
    return row + lanes


def _decorate_timeline_cell(
    char: str,
    color: int,
    attr: int,
    start: datetime,
    end: datetime,
    now: Optional[datetime],
) -> Tuple[str, int, int]:
    if now is None:
        return char, color, attr
    if start <= now < end:
        return NOW_CHAR, COLOR_SELECTED, attr | curses.A_BOLD
    if end <= now:
        return char, color, attr | curses.A_DIM
    return char, color, attr


def _draw_process_panel(
    stdscr,
    top: int,
    width: int,
    height: int,
    gpu: GpuSnapshot,
    usage: Sequence[ProcessUsage],
) -> None:
    if top >= height - 2:
        return
    util = f"{gpu.utilization_percent}%" if gpu.utilization_percent is not None else "n/a"
    if gpu.memory_total_mb:
        memory = f"{gpu.memory_used_mb / 1024:.1f}/{gpu.memory_total_mb / 1024:.0f}G"
    else:
        memory = "n/a"
    violations = sum(1 for item in usage if item.violation)
    title = f"Processes GPU {gpu.index} | util={util} mem={memory} source={gpu.source}"
    if violations:
        title += f" | violations={violations}"
    _addstr(stdscr, top, 0, title.ljust(width), width, COLOR_HEADER)
    if top + 1 >= height - 2:
        return
    _addstr(stdscr, top + 1, 0, _process_table_header(width), width, COLOR_MUTED)
    if not usage:
        if top + 2 < height - 2:
            _addstr(stdscr, top + 2, 0, "  no GPU processes", width, COLOR_MUTED)
        return
    rows = max(0, height - top - 4)
    for offset, item in enumerate(usage[:rows]):
        color = COLOR_ERROR if item.violation else (COLOR_MUTED if item.status in {"unknown", "system"} else COLOR_MINE)
        attr = curses.A_BOLD if item.violation else 0
        _addstr(stdscr, top + 2 + offset, 0, _process_table_line(item, width), width, color, attr)


def _process_table_header(width: int) -> str:
    if width < 100:
        return f"{'PID':>7} {'User':<10} {'T':<3} {'SM':>3} {'Mem':>6} {'State':<11} Command"
    return f"{'PID':>7} {'User':<16} {'Type':<4} {'SM':>4} {'Memory':>8} {'State':<11} {'Booking':<8} Command"


def _process_table_line(item: ProcessUsage, width: int) -> str:
    process = item.process
    sm = f"{process.sm_utilization_percent}%" if process.sm_utilization_percent is not None else "-"
    memory = f"{process.gpu_memory_mb}M" if process.gpu_memory_mb else "-"
    booking = ",".join(value[:6] for value in item.reservation_ids) or "-"
    command = summarize_process_command(process.command)
    if width < 100:
        prefix = (
            f"{process.pid:>7} {_truncate(process.username, 10):<10} "
            f"{process.kind:<3} {sm:>3} {memory:>6} {item.status:<11} "
        )
    else:
        prefix = (
            f"{process.pid:>7} {_truncate(process.username, 16):<16} "
            f"{process.kind:<4} {sm:>4} {memory:>8} {item.status:<11} {booking:<8} "
        )
    return prefix + _truncate(command, max(1, width - len(prefix) - 1))


def _cell_for_gpu(
    gpu: int,
    color_map: dict[str, int],
    active: Sequence[dict],
    start: datetime,
    end: datetime,
    selected_id: Optional[str],
    col: int = 0,
    lane: int = 0,
    lane_count: int = 1,
    reservation_index: Optional[ReservationIndex] = None,
) -> Tuple[str, int, int]:
    if reservation_index is None:
        overlapping = sorted(
            [
                item
                for item in active
                if gpu in item.get("gpus", [])
                and parse_iso(item["start_at"]) < end
                and start < parse_iso(item["end_at"])
            ],
            key=lambda item: (parse_iso(item["start_at"]), parse_iso(item["end_at"]), str(item.get("id", ""))),
        )
    else:
        overlapping = [item.record for item in reservation_index.overlapping(gpu, start, end)]
    if not overlapping:
        return FREE_CHAR, COLOR_FREE, 0
    exclusive = _choose_selected_or_first([item for item in overlapping if item.get("mode") == MODE_EXCLUSIVE], selected_id)
    if exclusive is not None:
        if selected_id and exclusive.get("id") == selected_id:
            return BAR_CHAR, _reservation_color(exclusive, color_map), _selected_bar_attr()
        return BAR_CHAR, _reservation_color(exclusive, color_map), curses.A_BOLD
    shared_items = [item for item in overlapping if item.get("mode") == MODE_SHARED]
    chosen = _choose_selected_or_first(shared_items, selected_id)
    if chosen is None:
        return FREE_CHAR, COLOR_FREE, 0
    if len(shared_items) == 1:
        if selected_id and chosen.get("id") == selected_id:
            return BAR_CHAR, _reservation_color(chosen, color_map), _selected_bar_attr()
        return BAR_CHAR, _reservation_color(chosen, color_map), curses.A_BOLD

    if len(shared_items) == 2 and lane_count <= 1:
        top_color = _reservation_color(shared_items[0], color_map)
        bottom_color = _reservation_color(shared_items[1], color_map)
        mixed_color = _mixed_color_pair(top_color, bottom_color)
        if mixed_color is not None:
            attr = curses.A_BOLD
            if selected_id and any(item.get("id") == selected_id for item in shared_items):
                attr |= curses.A_BLINK
            return SPLIT_CHAR, mixed_color, attr

    if len(shared_items) > 2 and lane_count <= 1:
        top_item, bottom_item = _shared_weave_pair(shared_items, col)
        top_color = _reservation_color(top_item, color_map)
        bottom_color = _reservation_color(bottom_item, color_map)
        mixed_color = _mixed_color_pair(top_color, bottom_color)
        if mixed_color is not None:
            attr = curses.A_BOLD
            if selected_id and selected_id in {top_item.get("id"), bottom_item.get("id")}:
                attr |= curses.A_BLINK
            return WEAVE_CHARS[col % len(WEAVE_CHARS)], mixed_color, attr

    visible = _shared_lane_item(shared_items, selected_id, col, lane, lane_count)
    attr = curses.A_BOLD
    if selected_id and visible.get("id") == selected_id:
        attr |= curses.A_BLINK
    char = SHARED_CHAR if lane_count <= 1 else BAR_CHAR
    return char, _reservation_color(visible, color_map), attr


def _preview_cell_for_gpu(
    gpu: int,
    start: datetime,
    end: datetime,
    preview: Optional[AddPreview],
    lane: int = 0,
    lane_count: int = 1,
) -> Optional[Tuple[str, int, int]]:
    if preview is None:
        return None
    if gpu not in preview.selected_gpus:
        return None
    if not (preview.start < end and start < preview.end):
        return None
    color = _preview_color(preview.mode) if preview.valid else COLOR_ERROR
    attr = curses.A_BOLD | (curses.A_BLINK if preview.blink else 0)
    return BAR_CHAR, color, attr


def _draw_reservation_panel(
    stdscr,
    top: int,
    width: int,
    height: int,
    store: LedgerStore,
    active: Sequence[dict],
    state: TuiState,
    selected_id: Optional[str],
    shared_limit: int,
    color_map: dict[str, int],
) -> None:
    actor = _current_actor()
    mine = [item for item in active if int(item.get("uid")) == actor.uid]
    if state.selected >= len(mine):
        state.selected = max(0, len(mine) - 1)

    mine_index = {reservation["id"]: index + 1 for index, reservation in enumerate(mine)}
    _addstr(stdscr, top, 0, "Reservations".ljust(width), width, COLOR_HEADER)
    header = _table_header(width)
    _addstr(stdscr, top + 1, 0, header, width, COLOR_MUTED)
    if not active:
        _addstr(stdscr, top + 2, 0, "  none", width, COLOR_MUTED)
        return

    rows = max(1, height - top - 4)
    for offset, reservation in enumerate(active[:rows]):
        row = top + 2 + offset
        own_number = mine_index.get(reservation["id"])
        prefix = f">{own_number}" if reservation.get("id") == selected_id else (str(own_number) if own_number else "-")
        line = _reservation_table_line(reservation, prefix, width, active, shared_limit)
        selected = reservation.get("id") == selected_id
        color = _reservation_color(reservation, color_map)
        _addstr(stdscr, row, 0, line, width, color, curses.A_BOLD if selected else 0)


def _draw_footer(stdscr, height: int, width: int, state: TuiState, preview: Optional[AddPreview]) -> None:
    if state.editor_active and preview is not None:
        operation = "edit" if state.edit_mode else "add"
        message = state.message or _preview_status_text(preview, operation)
        message_color = COLOR_ERROR if state.error or not preview.valid else _preview_color(preview.mode)
    elif state.focus == FOCUS_GPUS:
        message = state.message or f"GPU {state.selected_gpu} focus"
        message_color = COLOR_ERROR if state.error else COLOR_SELECTED
    else:
        message = state.message
        message_color = COLOR_ERROR if state.error else COLOR_MUTED
    footer = _footer_label(state, preview, width)
    _addstr(stdscr, height - 2, 0, message[: width - 1], width, message_color)
    _addstr(stdscr, height - 1, 0, footer.ljust(width), width, COLOR_HEADER)


def _footer_label(state: TuiState, preview: Optional[AddPreview], width: int) -> str:
    if state.editor_active and preview is not None:
        operation = "ADD" if state.add_mode else "EDIT"
        short = f" {operation} | 1-9 auto | f any | g selected | r reset | Enter/Esc | ? help "
        long = (
            f" {operation} | arrows move | Space GPU | +/- duration | m memory | s/x mode "
            "| 1-9 auto | f any GPUs | g selected GPUs | r reset | Enter/Esc | ? help "
        )
    elif state.focus == FOCUS_GPUS:
        short = " GPU | up/down | Tab RSV | a add | <-/-> pan | n NOW | ? help | q quit "
        long = (
            " GPU FOCUS | up/down select GPU | Tab reservations | a add here "
            "| +/- zoom | <-/-> history/future | n NOW | r refresh | ? help | q quit "
        )
    else:
        short = " RSV | up/down | Tab GPU | a/e/d | <-/-> pan | n NOW | ? help | q quit "
        long = (
            " RESERVATIONS | up/down select | Tab GPUs | a add | e edit | d delete "
            f"| +/- zoom {state.slot_minutes}m/col | <-/-> history/future | n NOW "
            "| r refresh | ? help | q quit "
        )
    footer = long if len(long) < width else short
    return footer[: max(0, width - 1)]


def _start_add_select(config: Config, state: TuiState) -> None:
    now = utc_now()
    view_start = _default_timeline_view_start(now, state.slot_minutes)
    booking_start = _ceil_to_add_step(now)
    state.add_mode = True
    state.edit_mode = False
    state.edit_reservation_id = None
    state.editor_view_start = view_start
    state.add_cursor_gpu = min(max(state.add_cursor_gpu, 0), max(0, config.gpu_count - 1))
    state.add_start_steps = max(
        0,
        int((booking_start - view_start).total_seconds() // (ADD_STEP_MINUTES * 60)),
    )
    state.add_duration_steps = max(1, state.add_duration_steps)
    state.add_selected_gpus = {state.add_cursor_gpu} if config.gpu_count else set()
    state.add_booking_mode = MODE_SHARED
    state.add_expected_memory_mb = None
    state.message = "Add: press 1-9 or f to auto-find; arrows and Space adjust"
    state.error = False


def _start_edit_select(config: Config, store: LedgerStore, state: TuiState) -> None:
    mine = _own_reservations(store)
    if not mine:
        state.message = "no reservation to edit"
        state.error = True
        return
    state.selected = min(max(0, state.selected), len(mine) - 1)
    reservation = mine[state.selected]
    if parse_iso(reservation["start_at"]) <= utc_now():
        state.message = "cannot edit a reservation after it has started"
        state.error = True
        return
    _load_edit_state(config, state, reservation)


def _load_edit_state(config: Config, state: TuiState, reservation: dict) -> None:
    start = parse_iso(reservation["start_at"])
    end = parse_iso(reservation["end_at"])
    gpus = {
        int(gpu)
        for gpu in reservation.get("gpus", [])
        if 0 <= int(gpu) < config.gpu_count
    }
    state.add_mode = False
    state.edit_mode = True
    state.edit_reservation_id = str(reservation["id"])
    state.editor_view_start = start - timedelta(minutes=30)
    state.add_start_steps = 6
    state.add_duration_steps = max(1, int((end - start).total_seconds() // (ADD_STEP_MINUTES * 60)))
    state.add_selected_gpus = gpus
    state.add_cursor_gpu = min(gpus) if gpus else 0
    state.add_booking_mode = reservation.get("mode") if reservation.get("mode") in {MODE_SHARED, MODE_EXCLUSIVE} else MODE_SHARED
    raw_memory = reservation.get("expected_memory_mb")
    state.add_expected_memory_mb = int(raw_memory) if raw_memory is not None else None
    state.message = ""
    state.error = False


def _handle_add_key(
    key: int,
    config: Config,
    store: LedgerStore,
    state: TuiState,
    *,
    stdscr=None,
) -> None:
    operation = "edit" if state.edit_mode else "add"
    if key in (ord("?"), ord("p"), ord("P")):
        if stdscr is None:
            state.message = "help: f any GPUs, g selected GPUs, r reset, Enter submit"
            state.error = False
        else:
            _help_dialog(stdscr, initial_page=1)
        return
    if key in (27, ord("q"), ord("Q")):
        _close_editor(state)
        state.message = f"{operation} cancelled"
        state.error = False
        return
    if key in (curses.KEY_RIGHT, ord("l"), ord("L")):
        state.add_start_steps += 1
        _clear_editor_feedback(state)
        return
    if key in (curses.KEY_LEFT, ord("h"), ord("H")):
        if state.add_start_steps > 0:
            state.add_start_steps -= 1
        elif state.edit_mode and state.editor_view_start is not None:
            state.editor_view_start -= timedelta(minutes=ADD_STEP_MINUTES)
        _clear_editor_feedback(state)
        return
    if key in (curses.KEY_DOWN, ord("j"), ord("J")):
        state.add_cursor_gpu = min(max(0, config.gpu_count - 1), state.add_cursor_gpu + 1)
        _clear_editor_feedback(state)
        return
    if key in (curses.KEY_UP, ord("k"), ord("K")):
        state.add_cursor_gpu = max(0, state.add_cursor_gpu - 1)
        _clear_editor_feedback(state)
        return
    if key == ord(" "):
        if state.add_cursor_gpu in state.add_selected_gpus:
            state.add_selected_gpus.remove(state.add_cursor_gpu)
        elif 0 <= state.add_cursor_gpu < config.gpu_count:
            state.add_selected_gpus.add(state.add_cursor_gpu)
        _clear_editor_feedback(state)
        return
    if key in (ord("["), ord("-"), ord("_")):
        state.add_duration_steps = max(1, state.add_duration_steps - 1)
        _clear_editor_feedback(state)
        return
    if key in (ord("]"), ord("+"), ord("=")):
        state.add_duration_steps += 1
        _clear_editor_feedback(state)
        return
    if key in (ord("s"), ord("S")):
        state.add_booking_mode = MODE_SHARED
        _clear_editor_feedback(state)
        return
    if key in (ord("x"), ord("X")):
        state.add_booking_mode = MODE_EXCLUSIVE
        _clear_editor_feedback(state)
        return
    if key in (ord("m"), ord("M")):
        if stdscr is None:
            state.message = "press m in the live TUI to enter expected memory"
            state.error = False
            return
        default = _memory_input_text(state.add_expected_memory_mb)
        raw = _prompt_line(stdscr, "Memory per GPU (12g/4096m, - clears)", default)
        if raw == "-":
            state.add_expected_memory_mb = None
        elif raw:
            try:
                state.add_expected_memory_mb = parse_memory_mb(raw)
            except ValueError as exc:
                state.message = str(exc)
                state.error = True
                return
        state.message = f"expected memory: {_editor_memory_label(state)}"
        state.error = False
        return
    if key in (ord("r"), ord("R")):
        _reset_editor(config, store, state)
        return
    if ord("1") <= key <= ord("9"):
        _find_add_slot(config, store, state, fixed_gpus=False, requested_count=key - ord("0"))
        return
    if key == ord("f"):
        _find_add_slot(config, store, state, fixed_gpus=False)
        return
    if key == ord("g"):
        _find_add_slot(config, store, state, fixed_gpus=True)
        return
    if key in (curses.KEY_ENTER, 10, 13):
        view_start = _editor_view_start(state)
        preview = _build_add_preview(store.load(), config, state, view_start)
        if not preview.valid:
            state.message = preview.reason
            state.error = True
            return
        try:
            if state.edit_mode:
                if not state.edit_reservation_id:
                    raise BookingError("reservation not found")
                result = edit_booking(
                    store,
                    config,
                    EditRequest(
                        actor=_current_actor(),
                        reservation_id=state.edit_reservation_id,
                        start_at=preview.start,
                        duration_seconds=int((preview.end - preview.start).total_seconds()),
                        mode=preview.mode,
                        preferred_gpus=list(preview.selected_gpus),
                        count=len(preview.selected_gpus),
                        allow_queue=False,
                        expected_memory_mb=state.add_expected_memory_mb,
                        update_expected_memory=True,
                        gpu_memory_capacity_mb=state.gpu_memory_capacity_mb,
                    ),
                )
            else:
                result = add_booking(
                    store,
                    config,
                    BookingRequest(
                        actor=_current_actor(),
                        count=len(preview.selected_gpus),
                        duration_seconds=int((preview.end - preview.start).total_seconds()),
                        start_at=preview.start,
                        mode=preview.mode,
                        preferred_gpus=list(preview.selected_gpus),
                        allow_queue=False,
                        expected_memory_mb=state.add_expected_memory_mb,
                        gpu_memory_capacity_mb=state.gpu_memory_capacity_mb,
                    ),
                )
        except BookingError as exc:
            state.message = str(exc)
            state.error = True
            return
        reservation_id = str(result.reservation["id"])
        _close_editor(state)
        state.focus = FOCUS_RESERVATIONS
        state.selected = _own_reservation_index(store, reservation_id)
        state.message = f"{'updated' if operation == 'edit' else 'created'} {reservation_id[:8]}"
        state.error = False


def _find_add_slot(
    config: Config,
    store: LedgerStore,
    state: TuiState,
    fixed_gpus: bool,
    requested_count: Optional[int] = None,
) -> None:
    selected = sorted(gpu for gpu in state.add_selected_gpus if 0 <= gpu < config.gpu_count)
    if fixed_gpus and not selected:
        state.message = "select at least one GPU before fixed search"
        state.error = True
        return

    count = requested_count if requested_count is not None else (len(selected) or 1)
    if count < 1 or count > config.gpu_count:
        state.message = f"GPU count must be between 1 and {config.gpu_count}"
        state.error = True
        return
    view_start = _editor_view_start(state)
    selected_start = view_start + timedelta(minutes=state.add_start_steps * ADD_STEP_MINUTES)
    earliest_start = max(selected_start, _ceil_to_add_step(utc_now()))
    duration = timedelta(minutes=max(1, state.add_duration_steps) * ADD_STEP_MINUTES)
    mode = state.add_booking_mode if state.add_booking_mode in {MODE_SHARED, MODE_EXCLUSIVE} else MODE_SHARED
    ledger = _availability_ledger(store.load(), state)
    advice = build_gpu_advice(config)
    if advice.memory_capacities_mb:
        state.gpu_memory_capacity_mb = advice.memory_capacities_mb
    allocator = (
        apply_external_allocator(
            config,
            store,
            _current_actor(),
            advice,
            count=count,
            duration_seconds=int(duration.total_seconds()),
            start_at=earliest_start,
            mode=mode,
            expected_memory_mb=state.add_expected_memory_mb,
        )
        if not fixed_gpus
        else AllocatorDecision(list(advice.order), dict(advice.scores), "fixed-gpu")
    )
    slot = find_earliest_slot(
        ledger,
        config,
        count,
        earliest_start,
        duration,
        mode,
        _current_actor().uid,
        preferred_gpus=selected if fixed_gpus else None,
        allow_queue=True,
        gpu_order=allocator.order,
        gpu_scores=allocator.scores,
        expected_memory_mb=state.add_expected_memory_mb,
        gpu_memory_capacity_mb=state.gpu_memory_capacity_mb,
    )
    if slot is None:
        scope = "selected GPUs" if fixed_gpus else f"{count} GPU"
        state.message = f"no {mode} slot for {scope} in the next {config.queue_search_hours}h"
        state.error = True
        return

    scheduled_start, gpus = slot
    state.add_selected_gpus = set(gpus)
    state.add_cursor_gpu = min(gpus)
    distance_steps = int((scheduled_start - view_start).total_seconds() // (ADD_STEP_MINUTES * 60))
    if distance_steps <= 6:
        state.add_start_steps = max(0, distance_steps)
    else:
        state.editor_view_start = scheduled_start - timedelta(minutes=30)
        state.add_start_steps = 6
    local_start = scheduled_start.astimezone()
    gpu_text = ",".join(map(str, gpus))
    search_kind = "fixed" if fixed_gpus else "auto"
    state.message = (
        f"{search_kind} found {count} GPU [{gpu_text}] at "
        f"{_weekday_label(local_start)} {local_start:%m-%d %H:%M}; "
        f"{allocator.source}; Enter confirms"
    )
    if allocator.warning:
        state.message = f"{state.message} ({allocator.warning})"
    state.error = False


def _reset_editor(config: Config, store: LedgerStore, state: TuiState) -> None:
    if state.edit_mode and state.edit_reservation_id:
        for reservation in store.load().get("reservations", []):
            if reservation.get("id") == state.edit_reservation_id:
                _load_edit_state(config, state, reservation)
                state.message = "edit reset to original"
                return
        state.message = "reservation not found"
        state.error = True
        return
    cursor_gpu = state.add_cursor_gpu
    state.add_duration_steps = 6
    _start_add_select(config, state)
    state.add_cursor_gpu = min(max(cursor_gpu, 0), max(0, config.gpu_count - 1))
    state.add_selected_gpus = {state.add_cursor_gpu} if config.gpu_count else set()
    state.message = "add reset to 1 GPU / 30m / shared"


def _availability_ledger(ledger: dict, state: TuiState) -> dict:
    if not state.edit_mode or not state.edit_reservation_id:
        return ledger
    return {
        **ledger,
        "reservations": [
            item
            for item in ledger.get("reservations", [])
            if item.get("id") != state.edit_reservation_id
        ],
    }


def _clear_editor_feedback(state: TuiState) -> None:
    state.message = ""
    state.error = False


def _close_editor(state: TuiState) -> None:
    state.add_mode = False
    state.edit_mode = False
    state.edit_reservation_id = None
    state.editor_view_start = None


def _editor_view_start(state: TuiState) -> datetime:
    if state.editor_view_start is not None:
        return state.editor_view_start
    return _default_timeline_view_start(utc_now(), state.slot_minutes)


def _own_reservation_index(store: LedgerStore, reservation_id: str) -> int:
    for index, reservation in enumerate(_own_reservations(store)):
        if reservation.get("id") == reservation_id:
            return index
    return 0


def _delete_selected(stdscr, store: LedgerStore, state: TuiState) -> None:
    mine = _own_reservations(store)
    if not mine:
        state.message = "no reservation to delete"
        state.error = True
        return
    reservation = mine[state.selected]
    answer = _prompt_line(stdscr, f"Delete {reservation['id'][:8]}? type yes", "")
    if answer != "yes":
        state.message = "delete cancelled"
        state.error = False
        return
    cancel_booking(store, reservation["id"], _current_actor())
    state.message = f"deleted {reservation['id'][:8]}"
    state.error = False


def _prompt_fields(stdscr, title: str, fields: Sequence[Tuple[str, str]]) -> Optional[List[str]]:
    height, width = stdscr.getmaxyx()
    win_height = min(height - 4, len(fields) + 4)
    win_width = min(width - 4, 72)
    top = max(1, (height - win_height) // 2)
    left = max(1, (width - win_width) // 2)
    win = curses.newwin(win_height, win_width, top, left)
    win.keypad(True)
    values: List[str] = []
    try:
        curses.curs_set(1)
        win.box()
        _win_addstr(win, 1, 2, title[: win_width - 4])
        for index, (label, default) in enumerate(fields):
            prompt = f"{label} [{default}]: " if default else f"{label}: "
            _win_addstr(win, index + 2, 2, prompt[: win_width - 4])
            curses.echo()
            raw = win.getstr(index + 2, min(win_width - 3, 2 + len(prompt)), max(0, win_width - len(prompt) - 5))
            curses.noecho()
            text = raw.decode("utf-8").strip()
            values.append(text or default)
        return values
    except KeyboardInterrupt:
        return None
    finally:
        curses.noecho()
        curses.curs_set(0)


def _prompt_line(stdscr, prompt: str, default: str = "") -> str:
    values = _prompt_fields(stdscr, "Confirm", [(prompt, default)])
    if values is None:
        return ""
    return values[0]


def _help_dialog(stdscr, initial_page: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    page = min(max(0, initial_page), len(HELP_PAGES) - 1)
    max_rows = max(len(entries) for _title, entries in HELP_PAGES)
    win_height = min(height - 2, max(16, max_rows + 4))
    win_width = min(width - 2, 96)
    top = max(1, (height - win_height) // 2)
    left = max(1, (width - win_width) // 2)
    win = curses.newwin(win_height, win_width, top, left)
    win.keypad(True)

    while True:
        win.erase()
        win.box()
        title, entries = HELP_PAGES[page]
        key_width = min(
            max(14, max((len(key) for key, _description in entries), default=0) + 2),
            max(14, win_width // 3),
        )
        heading = f" GPUbk Help {page + 1}/{len(HELP_PAGES)}  {title} "
        _win_addstr(win, 0, 2, heading, COLOR_HEADER, curses.A_BOLD)
        for offset, (key_label, description) in enumerate(entries):
            row = offset + 2
            if row >= win_height - 2:
                break
            if not key_label:
                _win_addstr(win, row, 2, description, COLOR_SELECTED, curses.A_BOLD)
                continue
            _win_addstr(win, row, 3, key_label, COLOR_PREVIEW_SHARED, curses.A_BOLD)
            _win_addstr(win, row, 3 + key_width, description, COLOR_MUTED)
        footer = " Left/Right page   1-4 jump   q/Esc/? close "
        _win_addstr(win, win_height - 2, 2, footer, COLOR_HEADER, curses.A_BOLD)
        win.refresh()
        key = win.getch()
        if key in (27, ord("q"), ord("Q"), ord("?"), curses.KEY_ENTER, 10, 13):
            return
        if key in (curses.KEY_RIGHT, ord("l"), ord("L"), 9):
            page = (page + 1) % len(HELP_PAGES)
            continue
        if key in (curses.KEY_LEFT, ord("h"), ord("H"), curses.KEY_BTAB):
            page = (page - 1) % len(HELP_PAGES)
            continue
        if ord("1") <= key < ord("1") + len(HELP_PAGES):
            page = key - ord("1")


def _message_dialog(stdscr, title: str, lines: Sequence[str]) -> None:
    height, width = stdscr.getmaxyx()
    win_height = min(height - 4, len(lines) + 4)
    win_width = min(width - 4, max(48, min(90, max(len(line) for line in lines) + 4)))
    top = max(1, (height - win_height) // 2)
    left = max(1, (width - win_width) // 2)
    win = curses.newwin(win_height, win_width, top, left)
    win.box()
    _win_addstr(win, 1, 2, title[: win_width - 4])
    for index, line in enumerate(lines):
        _win_addstr(win, index + 2, 2, line[: win_width - 4])
    _win_addstr(win, win_height - 2, 2, "press any key")
    win.refresh()
    win.getch()


def _normalized_snapshots(config: Config) -> List[GpuSnapshot]:
    items = snapshot(config)
    by_index = {item.index: item for item in items}
    result = []
    for index in range(config.gpu_count):
        result.append(by_index.get(index, GpuSnapshot(index=index, name="unknown")))
    return result


def _tiny_range(start: str, end: str) -> str:
    start_dt = parse_iso(start).astimezone()
    end_dt = parse_iso(end).astimezone()
    if start_dt.date() == end_dt.date():
        return f"{start_dt:%H:%M}-{end_dt:%H:%M}"
    return f"{start_dt:%m-%d %H:%M}->{end_dt:%m-%d %H:%M}"


def _build_add_preview(ledger: dict, config: Config, state: TuiState, view_start: datetime) -> AddPreview:
    cursor_gpu = min(max(state.add_cursor_gpu, 0), max(0, config.gpu_count - 1))
    selected = tuple(sorted(gpu for gpu in state.add_selected_gpus if 0 <= gpu < config.gpu_count))
    start = view_start + timedelta(minutes=state.add_start_steps * ADD_STEP_MINUTES)
    end = start + timedelta(minutes=max(1, state.add_duration_steps) * ADD_STEP_MINUTES)
    mode = state.add_booking_mode if state.add_booking_mode in {MODE_SHARED, MODE_EXCLUSIVE} else MODE_SHARED
    earliest = _ceil_to_add_step(utc_now())
    if start < earliest:
        local_earliest = earliest.astimezone()
        return AddPreview(
            start,
            end,
            selected,
            cursor_gpu,
            mode,
            False,
            f"start must be at or after {local_earliest:%m-%d %H:%M}",
            blink=state.add_mode,
        )
    if not selected:
        return AddPreview(
            start,
            end,
            selected,
            cursor_gpu,
            mode,
            False,
            "select at least one GPU with space",
            blink=state.add_mode,
        )
    availability_ledger = _availability_ledger(ledger, state)
    for gpu in selected:
        ok, reason = availability_detail(
            availability_ledger,
            gpu,
            start,
            end,
            mode,
            _current_actor().uid,
            config.max_shared_users,
            state.add_expected_memory_mb,
            state.gpu_memory_capacity_mb,
            config.shared_memory_reserve_mb,
        )
        if not ok:
            return AddPreview(start, end, selected, cursor_gpu, mode, False, reason, blink=state.add_mode)
    return AddPreview(start, end, selected, cursor_gpu, mode, True, blink=state.add_mode)


def _preview_status_text(preview: AddPreview, operation: str = "add") -> str:
    local_start = preview.start.astimezone()
    local_end = preview.end.astimezone()
    duration = _duration_text(local_end - local_start)
    gpu_text = ",".join(map(str, preview.selected_gpus)) or "-"
    status = "ok" if preview.valid else preview.reason
    return (
        f"{operation} {preview.mode} GPU={gpu_text} "
        f"{_weekday_label(local_start)} {local_start:%m-%d %H:%M}->{local_end:%m-%d %H:%M} "
        f"{duration} | {status}"
    )


def _memory_input_text(value: Optional[int]) -> str:
    if value is None:
        return ""
    if value % 1024 == 0:
        return f"{value // 1024}g"
    return f"{value}m"


def _editor_memory_label(state: TuiState) -> str:
    expected = "auto" if state.add_expected_memory_mb is None else _memory_compact(state.add_expected_memory_mb)
    selected_free = [
        state.gpu_memory_free_mb[gpu]
        for gpu in state.add_selected_gpus
        if gpu in state.gpu_memory_free_mb
    ]
    if selected_free:
        return f"Mem {expected} free {_memory_compact(min(selected_free))}"
    return f"Mem {expected}"


def _memory_compact(value: int) -> str:
    if value >= 1024:
        return f"{value / 1024:.1f}G"
    return f"{value}M"


def _preview_color(mode: str) -> int:
    return COLOR_PREVIEW_EXCLUSIVE if mode == MODE_EXCLUSIVE else COLOR_PREVIEW_SHARED


def _table_header(width: int) -> str:
    if width < 100:
        return f"{'#':>3} {'ID':<8} {'User':<10} {'M':<1} {'GPU':<8} {'Cap':<5} {'Start':<11} {'End':<11} {'Dur':<7}"
    return f"{'#':>3} {'ID':<8} {'User':<16} {'Mode':<4} {'GPU':<12} {'Cap':<5} {'Start':<11} {'End':<11} {'Dur':<7}"


def _reservation_table_line(reservation: dict, prefix: str, width: int, active: Sequence[dict], shared_limit: int) -> str:
    start = parse_iso(reservation["start_at"]).astimezone()
    end = parse_iso(reservation["end_at"]).astimezone()
    duration = _duration_text(end - start)
    user_width = 10 if width < 100 else 16
    gpu_width = 8 if width < 100 else 12
    mode_label = _mode_mark(reservation) if width < 100 else reservation.get("mode", "")[:4]
    cap = _capacity_text(reservation, active, shared_limit)
    return (
        f"{prefix:>3} {reservation['id'][:8]:<8} "
        f"{_truncate(str(reservation.get('username', '')), user_width):<{user_width}} "
        f"{mode_label:<{1 if width < 100 else 4}} "
        f"{_compact_gpus(reservation.get('gpus', []), gpu_width):<{gpu_width}} "
        f"{cap:<5} "
        f"{start:%m-%d %H:%M} {end:%m-%d %H:%M} {duration:<7}"
    )


def _capacity_text(reservation: dict, active: Sequence[dict], shared_limit: int) -> str:
    if reservation.get("mode") == MODE_EXCLUSIVE:
        return "-"
    peak = max_shared_record_count_for_reservation(active, reservation)
    return f"{peak}/{shared_limit}"


def _reservation_color_map(
    active: Sequence[dict],
    reservation_index: Optional[ReservationIndex] = None,
) -> dict[str, int]:
    if reservation_index is not None:
        return _indexed_reservation_color_map(reservation_index)
    assignments: dict[str, int] = {}
    ordered = sorted(active, key=lambda item: (parse_iso(item["start_at"]), parse_iso(item["end_at"]), str(item.get("id", ""))))
    palette = [COLOR_RES_BASE + offset for offset in range(len(RESERVATION_COLORS))]
    for index, item in enumerate(ordered):
        rid = str(item.get("id", ""))
        blocked = set()
        for other in ordered:
            other_id = str(other.get("id", ""))
            if other_id not in assignments:
                continue
            if not set(item.get("gpus", [])) & set(other.get("gpus", [])):
                continue
            if not _reservations_overlap(item, other):
                continue
            blocked.add(assignments[other_id])
        assignments[rid] = _pick_palette_color(palette, index, blocked)
    return assignments


def _indexed_reservation_color_map(index: ReservationIndex) -> dict[str, int]:
    assignments: dict[str, int] = {}
    active_by_gpu: dict[int, list[tuple[datetime, int, str]]] = {}
    palette = [COLOR_RES_BASE + offset for offset in range(len(RESERVATION_COLORS))]
    for preferred_index, span in enumerate(index.spans):
        rid = str(span.record.get("id", ""))
        blocked = set()
        for gpu in span.gpus:
            current = [item for item in active_by_gpu.get(gpu, []) if item[0] > span.start]
            active_by_gpu[gpu] = current
            blocked.update(item[1] for item in current if item[2] != rid)
        color = _pick_palette_color(palette, preferred_index, blocked)
        assignments[rid] = color
        for gpu in span.gpus:
            active_by_gpu.setdefault(gpu, []).append((span.end, color, rid))
    return assignments


def _pick_palette_color(palette: Sequence[int], preferred_index: int, blocked: set[int]) -> int:
    for offset in range(len(palette)):
        color = palette[(preferred_index + offset) % len(palette)]
        if color not in blocked:
            return color
    return palette[preferred_index % len(palette)]


def _reservation_palette(theme: Optional[str] = None) -> List[int]:
    try:
        if curses.COLORS >= 256:
            if (theme or ACTIVE_TUI_THEME) == "light":
                return list(LIGHT_RESERVATION_COLORS_256)
            return list(DARK_RESERVATION_COLORS_256)
    except curses.error:
        pass
    return list(RESERVATION_COLORS)


def _init_mixed_color_pairs(palette: Sequence[int]) -> None:
    MIXED_COLOR_PAIRS.clear()
    pair_id = COLOR_RES_BASE + len(palette)
    max_pairs = getattr(curses, "COLOR_PAIRS", 0)
    for top_index in range(len(palette)):
        for bottom_index in range(top_index + 1, len(palette)):
            if pair_id >= max_pairs:
                return
            try:
                curses.init_pair(pair_id, palette[top_index], palette[bottom_index])
            except curses.error:
                return
            top_color = COLOR_RES_BASE + top_index
            bottom_color = COLOR_RES_BASE + bottom_index
            MIXED_COLOR_PAIRS[(top_color, bottom_color)] = pair_id
            pair_id += 1


def _mixed_color_pair(top_color: int, bottom_color: int) -> Optional[int]:
    if top_color == bottom_color:
        return None
    return MIXED_COLOR_PAIRS.get(tuple(sorted((top_color, bottom_color))))


def _selected_bar_attr() -> int:
    return curses.A_BOLD | curses.A_BLINK


def _reservation_by_id(active: Sequence[dict], reservation_id: Optional[str]) -> Optional[dict]:
    if not reservation_id:
        return None
    for item in active:
        if item.get("id") == reservation_id:
            return item
    return None


def _share_detail_rows(
    height: int,
    row: int,
    peak: int,
    shared_limit: int,
    reserved_rows: int = 8,
) -> int:
    if peak <= 0:
        return 0
    available = max(0, height - row - reserved_rows)
    if available <= 0:
        return 0
    return min(max(1, peak), max(1, shared_limit), available)


def _visible_shared_reservations(
    active: Sequence[dict],
    gpu: int,
    view_start: datetime,
    view_end: datetime,
    reservation_index: Optional[ReservationIndex] = None,
) -> List[dict]:
    if reservation_index is not None:
        return [
            item.record
            for item in reservation_index.overlapping(gpu, view_start, view_end)
            if item.mode == MODE_SHARED
        ]
    return sorted(
        [
            item
            for item in active
            if item.get("mode") == MODE_SHARED
            and gpu in item.get("gpus", [])
            and parse_iso(item["start_at"]) < view_end
            and view_start < parse_iso(item["end_at"])
        ],
        key=lambda item: (
            parse_iso(item["start_at"]),
            parse_iso(item["end_at"]),
            str(item.get("id", "")),
        ),
    )


def _selected_share_detail(
    active: Sequence[dict],
    selected: dict,
    view_start: datetime,
    view_end: datetime,
) -> Optional[Tuple[int, List[dict]]]:
    candidates: List[Tuple[int, List[dict]]] = []
    for raw_gpu in selected.get("gpus", []):
        gpu = int(raw_gpu)
        related = _related_shared_reservations(active, selected, gpu, view_start, view_end)
        if len(related) > 1:
            candidates.append((gpu, related))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (len(item[1]), -item[0]))


def _related_shared_reservations(
    active: Sequence[dict],
    selected: dict,
    gpu: int,
    view_start: datetime,
    view_end: datetime,
) -> List[dict]:
    selected_id = selected.get("id")
    related = [
        item
        for item in active
        if item.get("mode") == MODE_SHARED
        and gpu in item.get("gpus", [])
        and _reservations_overlap(item, selected)
        and parse_iso(item["start_at"]) < view_end
        and view_start < parse_iso(item["end_at"])
    ]
    return sorted(
        related,
        key=lambda item: (
            0 if item.get("id") == selected_id else 1,
            parse_iso(item["start_at"]),
            parse_iso(item["end_at"]),
            str(item.get("id", "")),
        ),
    )


def _share_lane_label(gpu: int, lane: int, total: int, reservation: dict, hidden: int = 0) -> str:
    username = _truncate(str(reservation.get("username", "?")), 10)
    suffix = f" +{hidden}" if hidden else ""
    return f"G{gpu} {lane + 1}/{total} {str(reservation.get('id', ''))[:6]} {username}{suffix}"


def _peak_shared_count_for_gpu(
    active: Sequence[dict],
    gpu: int,
    start: datetime,
    end: datetime,
    reservation_index: Optional[ReservationIndex] = None,
) -> int:
    if reservation_index is not None:
        spans = [item for item in reservation_index.overlapping(gpu, start, end) if item.mode == MODE_SHARED]
        points = {start, end}
        for item in spans:
            points.add(max(start, item.start))
            points.add(min(end, item.end))
        ordered = sorted(points)
        return max(
            (
                sum(1 for item in spans if item.start < right and left < item.end)
                for left, right in zip(ordered, ordered[1:])
                if left < right
            ),
            default=0,
        )
    shared = [
        item
        for item in active
        if item.get("mode") == MODE_SHARED
        and gpu in item.get("gpus", [])
        and parse_iso(item["start_at"]) < end
        and start < parse_iso(item["end_at"])
    ]
    points = {start, end}
    for item in shared:
        points.add(max(start, parse_iso(item["start_at"])))
        points.add(min(end, parse_iso(item["end_at"])))
    peak = 0
    ordered = sorted(points)
    for left, right in zip(ordered, ordered[1:]):
        if left >= right:
            continue
        peak = max(
            peak,
            sum(1 for item in shared if parse_iso(item["start_at"]) < right and left < parse_iso(item["end_at"])),
        )
    return peak


def _gpu_label(
    gpu: GpuSnapshot,
    width: int,
    peak_shared: int = 0,
    shared_limit: int = 1,
    violations: int = 0,
) -> str:
    gpu_field = _truncate(f"GPU{gpu.index}", 5).ljust(5)
    capacity_field = _truncate(f"S{peak_shared}/{shared_limit}", 6).ljust(6)
    extras = [f"!{violations}"] if violations else []
    if not gpu.memory_total_mb and gpu.utilization_percent is None:
        core = f"{gpu_field} {capacity_field} no telemetry"
        return " ".join([core] + extras)[:width].ljust(width)

    util = f"U{gpu.utilization_percent}%" if gpu.utilization_percent is not None else "U-"
    memory = "-"
    if gpu.memory_total_mb:
        used_gib = gpu.memory_used_mb / 1024
        total_gib = gpu.memory_total_mb / 1024
        memory = f"{used_gib:.1f}/{total_gib:.0f}G"
    core = f"{gpu_field} {capacity_field} {util:<5} {_truncate(memory, 9):<9}"
    if gpu.temperature_c is not None:
        extras.append(f"{gpu.temperature_c}C")
    if gpu.processes:
        extras.append(f"P{len(gpu.processes)}")
    return " ".join([core] + extras)[:width].ljust(width)


def _shared_lane_item(
    shared_items: Sequence[dict],
    selected_id: Optional[str],
    col: int,
    lane: int,
    lane_count: int,
) -> dict:
    if lane_count <= 1 and selected_id:
        for item in shared_items:
            if item.get("id") == selected_id:
                return item
    if lane_count <= 1:
        return shared_items[col % len(shared_items)]
    lane_index = min(len(shared_items) - 1, (max(0, lane) * len(shared_items)) // max(1, lane_count))
    return shared_items[lane_index]


def _shared_weave_pair(shared_items: Sequence[dict], col: int) -> Tuple[dict, dict]:
    count = len(shared_items)
    if count < 2:
        raise ValueError("shared weave requires at least two reservations")
    if count % 2 == 0:
        pair_index = max(0, col) % (count // 2)
        item_index = pair_index * 2
        return shared_items[item_index], shared_items[item_index + 1]
    item_index = max(0, col) % count
    return shared_items[item_index], shared_items[(item_index + 1) % count]


def _choose_selected_or_first(items: Sequence[dict], selected_id: Optional[str]) -> Optional[dict]:
    if not items:
        return None
    if selected_id:
        for item in items:
            if item.get("id") == selected_id:
                return item
    return items[0]


def _reservations_overlap(left: dict, right: dict) -> bool:
    return parse_iso(left["start_at"]) < parse_iso(right["end_at"]) and parse_iso(right["start_at"]) < parse_iso(left["end_at"])


def _time_axis_lines(start: datetime, end: datetime, timeline_width: int) -> Tuple[str, str, str, str]:
    if timeline_width <= 0:
        return "", "", "", ""
    dates = [" "] * timeline_width
    hours = [" "] * timeline_width
    minutes = [" "] * timeline_width
    ruler = ["─"] * timeline_width
    ruler[0] = "╞"

    total = max(1.0, (end - start).total_seconds())
    slot_minutes = max(1, int(total // timeline_width // 60))
    minor_minutes, major_minutes = _axis_tick_minutes(slot_minutes)
    cursor = _floor_to_tick(start, minor_minutes)
    while cursor < start:
        cursor += timedelta(minutes=minor_minutes)

    start_local = start.astimezone()
    end_local = end.astimezone(start_local.tzinfo)
    date_boundaries = []
    midnight = start_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    while midnight < end_local:
        col = _time_col(midnight.astimezone(timezone.utc), start, total, timeline_width)
        date_boundaries.append((col, _date_label(midnight)))
        midnight += timedelta(days=1)
    start_date_label = _date_label(start_local)
    if not date_boundaries or date_boundaries[0][0] >= len(start_date_label) + 1:
        _place_label(dates, 0, start_date_label)
    date_occupied_until = 0
    for col, label in date_boundaries:
        if col >= date_occupied_until and col + len(label) <= timeline_width:
            _place_label(dates, col, label)
            date_occupied_until = col + len(label) + 1

    minutes_to_next_hour = (60 - start_local.minute) % 60
    cols_to_next_hour = minutes_to_next_hour / slot_minutes if minutes_to_next_hour else timeline_width
    hour_occupied_until = 0
    if start_local.minute and cols_to_next_hour >= len(_hour_label(start_local)) + 1:
        label = _hour_label(start_local)
        _place_label(hours, 0, label)
        hour_occupied_until = len(label) + 1

    minute_occupied_until = 0
    if start_local.minute % minor_minutes:
        next_tick_col = _time_col(cursor, start, total, timeline_width)
        label = _minute_label(start_local)
        if next_tick_col >= len(label) + 1:
            _place_label(minutes, 0, label)
            minute_occupied_until = len(label) + 1

    while cursor < end:
        col = _time_col(cursor, start, total, timeline_width)
        local = cursor.astimezone()
        if local.minute == 0:
            label = _hour_label(local)
            if col >= hour_occupied_until and col + len(label) <= timeline_width:
                _place_label(hours, col, label)
                hour_occupied_until = col + len(label) + 1
            ruler[col] = "╋"
        else:
            label = _minute_label(local)
            if col >= minute_occupied_until and col + len(label) <= timeline_width:
                _place_label(minutes, col, label)
                minute_occupied_until = col + len(label) + 1
            ruler[col] = "┿" if _is_tick_aligned(cursor, major_minutes) else "┬"
        cursor += timedelta(minutes=minor_minutes)

    return "".join(dates), "".join(hours), "".join(minutes), "".join(ruler)


def _timeline_now_col(
    now: Optional[datetime],
    start: datetime,
    end: datetime,
    timeline_width: int,
) -> Optional[int]:
    if now is None or timeline_width <= 0 or not (start <= now < end):
        return None
    return _time_col(now, start, max(1.0, (end - start).total_seconds()), timeline_width)


def _default_timeline_view_start(now: datetime, slot_minutes: int) -> datetime:
    anchor = _floor_to_add_step(now)
    return anchor - timedelta(minutes=NOW_CONTEXT_COLUMNS * max(1, slot_minutes))


def _timeline_view_start(now: datetime, state: TuiState) -> datetime:
    return _default_timeline_view_start(now, state.slot_minutes) + timedelta(
        minutes=state.offset_slots * state.slot_minutes
    )


def _axis_tick_minutes(slot_minutes: int) -> Tuple[int, int]:
    if slot_minutes <= 5:
        return 15, 30
    if slot_minutes <= 15:
        return 30, 60
    if slot_minutes <= 30:
        return 60, 120
    return 180, 360


def _floor_to_tick(value: datetime, tick_minutes: int) -> datetime:
    value = value.astimezone(timezone.utc).replace(second=0, microsecond=0)
    tick_seconds = tick_minutes * 60
    timestamp = int(value.timestamp())
    return datetime.fromtimestamp(timestamp - (timestamp % tick_seconds), timezone.utc)


def _time_col(value: datetime, start: datetime, total_seconds: float, timeline_width: int) -> int:
    cell_seconds = max(1.0, total_seconds / max(1, timeline_width))
    col = int((value - start).total_seconds() // cell_seconds)
    return min(max(0, col), timeline_width - 1)


def _is_tick_aligned(value: datetime, tick_minutes: int) -> bool:
    return int(value.astimezone(timezone.utc).timestamp()) % (tick_minutes * 60) == 0


def _place_label(chars: List[str], col: int, label: str) -> None:
    for idx, char in enumerate(label):
        target = col + idx
        if 0 <= target < len(chars):
            chars[target] = char


def _hour_label(value: datetime) -> str:
    return f"{value.hour}h"


def _minute_label(value: datetime) -> str:
    return f"{value.minute:02d}"


def _weekday_label(value: datetime) -> str:
    return WEEKDAY_LABELS[value.weekday()]


def _date_label(value: datetime) -> str:
    return f"{value:%m-%d} {_weekday_label(value)}"


def _duration_text(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h{mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "+"


def _mode_mark(reservation: dict) -> str:
    return "X" if reservation.get("mode") == MODE_EXCLUSIVE else "S"


def _reservation_color(reservation: dict, color_map: Optional[dict[str, int]] = None) -> int:
    raw = str(reservation.get("id", ""))
    if color_map and raw in color_map:
        return color_map[raw]
    value = sum(ord(char) for char in raw)
    return COLOR_RES_BASE + (value % len(RESERVATION_COLORS))


def _compact_gpus(gpus: Sequence[int], max_width: int) -> str:
    text = ",".join(str(item) for item in gpus)
    if len(text) <= max_width:
        return text
    return text[: max(0, max_width - 1)] + "+"


def _ceil_to_add_step(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc).replace(microsecond=0)
    timestamp = int(value.timestamp())
    step = ADD_STEP_MINUTES * 60
    remainder = timestamp % step
    if remainder:
        timestamp += step - remainder
    return datetime.fromtimestamp(timestamp, timezone.utc)


def _floor_to_add_step(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc).replace(second=0, microsecond=0)
    timestamp = int(value.timestamp())
    step = ADD_STEP_MINUTES * 60
    return datetime.fromtimestamp(timestamp - (timestamp % step), timezone.utc)


def _own_reservations(store: LedgerStore) -> List[dict]:
    actor = _current_actor()
    return [item for item in list_active(store.load()) if int(item.get("uid")) == actor.uid]


def _selected_reservation_id(active: Sequence[dict], state: TuiState) -> Optional[str]:
    actor = _current_actor()
    mine = [item for item in active if int(item.get("uid")) == actor.uid]
    if state.selected >= len(mine):
        state.selected = max(0, len(mine) - 1)
    if not mine:
        return None
    return str(mine[state.selected].get("id"))


def _timeline_selected_id(active: Sequence[dict], state: TuiState) -> Optional[str]:
    if state.editor_active or state.focus != FOCUS_RESERVATIONS:
        return None
    return _selected_reservation_id(active, state)


def _current_actor() -> Actor:
    return current_actor()


def _addstr(stdscr, row: int, col: int, text: str, width: int, color: int = 0, attr: int = 0) -> None:
    if row < 0 or col >= width:
        return
    if color in {COLOR_FREE, COLOR_MUTED}:
        attr |= curses.A_DIM
    render_attr = (curses.color_pair(color) if color else 0) | attr
    try:
        stdscr.addstr(row, col, text[: max(0, width - col - 1)], render_attr)
    except curses.error:
        pass


def _win_addstr(win, row: int, col: int, text: str, color: int = 0, attr: int = 0) -> None:
    try:
        height, width = win.getmaxyx()
        if 0 <= row < height and 0 <= col < width:
            if color in {COLOR_FREE, COLOR_MUTED}:
                attr |= curses.A_DIM
            render_attr = (curses.color_pair(color) if color else 0) | attr
            win.addstr(row, col, text[: max(0, width - col - 1)], render_attr)
    except curses.error:
        pass


def _print_fallback(config: Config, store: LedgerStore) -> None:
    now = utc_now()
    print("GPUbk TUI fallback")
    print(f"data={config.data_dir} shared_limit={config.max_shared_users}")
    print("active reservations:")
    for reservation in list_active(store.load(), now):
        gpus = ",".join(str(item) for item in reservation.get("gpus", []))
        print(f"- {reservation['id'][:8]} {reservation['mode']} GPU={gpus} {format_local_range(reservation['start_at'], reservation['end_at'])}")
