from __future__ import annotations

import curses
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

from .admin_info import administrator_display_lines, administrator_info
from .advisor import build_gpu_advice
from .announcements import active_announcements
from .allocator import AllocatorDecision, apply_external_allocator
from .config import Config
from .granularity import DEFAULT_SLOT_MINUTES, ceil_to_slot, floor_to_slot
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
    booking_policy_conflict,
    edit_booking,
    find_earliest_slot,
    find_nearest_slot,
    list_active,
    shared_capacity_units_for_gpu,
)
from .service import public_reservation, submit_cancellation
from .sharing import parse_share_units, reservation_share_units, share_text
from .storage import LedgerStore
from .terminal import clip_display_text, pad_display_text, wrap_display_text
from .timeparse import format_local, format_local_range, parse_iso, parse_memory_mb, utc_now
from .tutorial import TUI_TOUR, mark_onboarding_seen, onboarding_seen
from .usage import (
    ProcessUsage,
    classify_process_usage,
    process_container_label,
    process_owner_label,
    summarize_process_command,
)
from .usage_api import UsageQueryService
from .usage_store import UsageAuditStore
from .usage_view import (
    activity_bar_cells,
    build_activity_trends,
    format_usage_duration,
    format_usage_memory,
)
from .worker_status import inspect_worker_status, reservations_need_worker


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

MIN_SHORT_ID_WIDTH = 6
MAX_SHORT_ID_WIDTH = 12
BAR_CHAR = "█"
SHARED_CHAR = "▓"
SHARED_START_CHAR = "╺"
SHARED_END_CHAR = "╸"
SHARED_SHORT_CHAR = "◆"
SPLIT_CHAR = "▀"
LOWER_SPLIT_CHAR = "▄"
WEAVE_CHARS = ("▚", "▞")
FREE_CHAR = "."
NOW_CHAR = "│"
NOW_CONTEXT_COLUMNS = 6
ZOOM_LEVELS = [5, 10, 15, 30, 60, 120, 240, 480, 720, 1440]
DEFAULT_ZOOM_INDEX = 0
FOCUS_RESERVATIONS = "reservations"
FOCUS_GPUS = "gpus"
DEFAULT_TIMELINE_COLUMNS = 48
EDITOR_CONTEXT_COLUMNS = 3
EDITOR_MIN_QUICK_MINUTES = 30
ACCELERATED_MULTIPLIER = 6
ACCELERATED_ZOOM_LEVELS = 3
ACCELERATED_GPU_ROWS = 4
GPU_MAP_SIZE = 8
GPU_POSITION_MAP_MAX = 10
SPEED_LEVELS = (1, 6, 24)
SPEED_ZOOM_STEPS = (1, 2, 4)
KEY_SHIFT_LEFT = getattr(curses, "KEY_SLEFT", -1001)
KEY_SHIFT_RIGHT = getattr(curses, "KEY_SRIGHT", -1002)
KEY_SHIFT_UP = getattr(curses, "KEY_SR", -1003)
KEY_SHIFT_DOWN = getattr(curses, "KEY_SF", -1004)
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
            ("+ / -", "Zoom from finest slice to 1 day per column"),
            ("v", "Cycle the reliable adjustment speed: 1x, 6x, or 24x"),
            ("r", "Refresh now; auto-refresh uses configured interval"),
            ("c", "Toggle the dark or light color theme"),
            ("z", "Toggle capacity-sliced or solid-first shared bars"),
            ("", "FOCUS AND ACTIONS"),
            ("Up / Down", "Move through reservations or GPU rows"),
            ("Tab", "Switch between reservation and GPU focus"),
            ("Enter", "Inspect the selected reservation or GPU"),
            ("a / e / d", "Add, edit, or delete a reservation"),
            ("u", "Open your sampled GPU-use dashboard"),
            ("i", "Show the administrator account and contact"),
            ("?", "Open this help"),
            ("q / Esc", "Quit GPUBK"),
        ),
    ),
    (
        "Add / Edit",
        (
            ("1-9", "Set GPU count; find earliest from the search start"),
            ("f", "Earliest on any GPUs from NOW or selected start"),
            ("g", "Find earliest slot on exactly the selected GPUs"),
            ("o", "Nearest around cursor; ties prefer earlier"),
            ("Left / Right", "Move start time by one configured booking slice"),
            ("Up / Down", "Move the GPU cursor"),
            ("Space", "Select or clear the current GPU"),
            ("[ / ]", "Shorten or extend duration by one booking slice"),
            (", / .", "Quick duration down or up; step follows zoom"),
            ("+ / -", "Zoom while keeping the selected interval in view"),
            ("Shift", "Larger step for movement, duration, and zoom"),
            ("v", "Cycle 1x, 6x, or 24x when Shift is not reported"),
            ("s / x", "Choose shared or exclusive mode"),
            ("u", "Set the integer shared slots requested per GPU"),
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
            ("Shared", "GPU labels show used/max slots, such as 3/4"),
            ("OFF", "Disabled by admin; status and history stay visible"),
            ("z", "Solid bars until overlap; capacity slices otherwise"),
            ("GPU focus", "Tab to expand share lanes and live processes"),
            ("user", "Process owner verified from the host UID"),
            ("user*", "Container owner inferred from one reservation"),
            ("container?", "Container owner is ambiguous; no user is charged"),
            ("Reservation", "Select a row to blink its exact interval"),
            ("Monitor", "Header shows collector health; details: bk doctor"),
            ("Worker", "Header shows your scheduled-command worker"),
            ("Util history", "Press u for your dashboard; bk u offers detail"),
            ("Live context", "Run: bk agent context --compact"),
            ("Theme", "Auto-detect; set BK_TUI_THEME=dark or light"),
            ("", "PAST RESERVATIONS ARE READ-ONLY"),
        ),
    ),
    (
        "Quick Tour",
        (
            ("bk tutorial", "Replay the safe CLI walkthrough anytime"),
            ("bk 2 1h", "Book two shared GPUs at best available time"),
            ("bk 1 1h --mem 12g", "Book shared capacity with expected VRAM"),
            ("bk 1 1h --share 2", "Request two integer shared slots per GPU"),
            ("bk x 1 1h", "Book one GPU exclusively; x means exclusive"),
            ("bk tui", "Open this timeline"),
            ("i", "Show the administrator account and contact"),
            ("a then 2", "Find earliest two-GPU slot; Enter confirms"),
            ("Tab", "Inspect a GPU's sharers and processes"),
            ("e", "Edit future fields; running jobs are end-only"),
            ("bk u", "Show this UID's historical GPU summary"),
            ("u in TUI", "Open your personal summary without leaving TUI"),
            ("bk agent context", "Give an Agent safe allocation context"),
            ("bk doctor", "Read-only policy and ledger diagnostics"),
        ),
    ),
)
@dataclass
class TuiState:
    offset_slots: int = 0
    zoom_index: int = DEFAULT_ZOOM_INDEX
    selected: int = -1
    focus: str = FOCUS_RESERVATIONS
    selected_gpu: int = 0
    timeline_style: str = "capacity"
    message: str = ""
    error: bool = False
    add_mode: bool = False
    edit_mode: bool = False
    edit_started: bool = False
    edit_reservation_id: Optional[str] = None
    editor_view_start: Optional[datetime] = None
    add_search_anchor: Optional[datetime] = None
    add_cursor_gpu: int = 0
    add_start_steps: int = 0
    add_duration_steps: int = 6
    add_selected_gpus: set[int] = field(default_factory=set)
    add_booking_mode: str = MODE_SHARED
    add_expected_memory_mb: Optional[int] = None
    add_share_units: int = 1
    gpu_memory_capacity_mb: dict[int, int] = field(default_factory=dict)
    gpu_memory_free_mb: dict[int, int] = field(default_factory=dict)
    timeline_columns: int = DEFAULT_TIMELINE_COLUMNS
    speed_index: int = 0
    booking_slot_minutes: int = DEFAULT_SLOT_MINUTES
    zoom_levels: Tuple[int, ...] = tuple(ZOOM_LEVELS)
    collector_status: dict = field(
        default_factory=lambda: {"state": "not-seen", "fresh": None}
    )
    collector_checked_at: Optional[datetime] = None
    worker_status: dict = field(
        default_factory=lambda: {"state": "idle", "running": None}
    )
    worker_checked_at: Optional[datetime] = None

    @property
    def slot_minutes(self) -> int:
        return self.zoom_levels[self.zoom_index]

    @property
    def editor_active(self) -> bool:
        return self.add_mode or self.edit_mode

    @property
    def speed_multiplier(self) -> int:
        return SPEED_LEVELS[self.speed_index]


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
    share_units: int = 1
    share_capacity: int = 1


def run_tui(
    config: Config,
    store: LedgerStore,
    *,
    show_tutorial: bool = False,
) -> int:
    first_tour = show_tutorial
    if not first_tour:
        try:
            first_tour = not onboarding_seen(TUI_TOUR)
        except (OSError, ValueError):
            first_tour = False
    try:
        return curses.wrapper(_run, config, store, first_tour)
    except curses.error:
        _print_fallback(config, store)
        return 0


def _run(
    stdscr,
    config: Config,
    store: LedgerStore,
    show_tutorial: bool = False,
) -> int:
    _init_curses(stdscr, config.tui_refresh_seconds)
    if show_tutorial:
        _help_dialog(stdscr, initial_page=0, tutorial=True)
        try:
            mark_onboarding_seen(TUI_TOUR)
        except (OSError, ValueError):
            pass
    state = TuiState(
        booking_slot_minutes=config.slot_minutes,
        add_duration_steps=_default_editor_duration_steps(config.slot_minutes),
    )
    _configure_booking_slot(state, config.slot_minutes)
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


def _init_curses(stdscr, refresh_seconds: float = 1.0) -> None:
    curses.curs_set(0)
    stdscr.timeout(max(1, round(refresh_seconds * 1000)))
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
        state.collector_checked_at = None
        state.worker_checked_at = None
        state.message = (
            f"refreshed now (automatic refresh: {config.tui_refresh_seconds:g}s)"
        )
        state.error = False
        return
    if key in (ord("c"), ord("C")):
        next_theme = "light" if ACTIVE_TUI_THEME == "dark" else "dark"
        _apply_tui_theme(next_theme)
        state.message = f"theme: {ACTIVE_TUI_THEME} (set BK_TUI_THEME to make it persistent)"
        state.error = False
        return
    if key in (ord("z"), ord("Z")):
        state.timeline_style = (
            "solid" if state.timeline_style == "capacity" else "capacity"
        )
        state.message = f"shared timeline style: {state.timeline_style}"
        state.error = False
        return
    if key in (ord("v"), ord("V")):
        _cycle_speed(state)
        return
    if key in (KEY_SHIFT_RIGHT, ord("L")):
        _pan_timeline(config, state, 1, state.speed_multiplier * ACCELERATED_MULTIPLIER)
        return
    if key in (KEY_SHIFT_LEFT, ord("H")):
        _pan_timeline(config, state, -1, state.speed_multiplier * ACCELERATED_MULTIPLIER)
        return
    if key in (curses.KEY_RIGHT, ord("l")):
        _pan_timeline(config, state, 1, state.speed_multiplier)
        return
    if key in (curses.KEY_LEFT, ord("h")):
        _pan_timeline(config, state, -1, state.speed_multiplier)
        return
    if key in (ord("n"), ord("N"), curses.KEY_HOME):
        state.offset_slots = 0
        state.message = "live NOW window"
        state.error = False
        return
    if key == ord("+"):
        _change_zoom(state, -_speed_zoom_step(state) * ACCELERATED_ZOOM_LEVELS)
        state.message = f"fast zoom {state.slot_minutes}m/col"
        state.error = False
        return
    if key == ord("="):
        _change_zoom(state, -_speed_zoom_step(state))
        state.message = f"zoom {state.slot_minutes}m/col"
        state.error = False
        return
    if key == ord("_"):
        _change_zoom(state, _speed_zoom_step(state) * ACCELERATED_ZOOM_LEVELS)
        state.message = f"fast zoom {state.slot_minutes}m/col"
        state.error = False
        return
    if key == ord("-"):
        _change_zoom(state, _speed_zoom_step(state))
        state.message = f"zoom {state.slot_minutes}m/col"
        state.error = False
        return
    if key == 9:
        _toggle_focus(config, store, state)
        return
    if key in (KEY_SHIFT_DOWN, ord("J")):
        _move_focus_down(config, store, state, state.speed_multiplier * ACCELERATED_GPU_ROWS)
        return
    if key in (KEY_SHIFT_UP, ord("K")):
        _move_focus_up(config, store, state, state.speed_multiplier * ACCELERATED_GPU_ROWS)
        return
    if key in (curses.KEY_DOWN, ord("j")):
        _move_focus_down(config, store, state, state.speed_multiplier)
        return
    if key in (curses.KEY_UP, ord("k")):
        _move_focus_up(config, store, state, state.speed_multiplier)
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
        _delete_selected(stdscr, config, store, state)
        return
    if key in (curses.KEY_ENTER, 10, 13):
        if state.focus == FOCUS_GPUS:
            if stdscr is None:
                state.message = f"GPU {state.selected_gpu} details"
            else:
                _show_gpu_details(stdscr, config, store, state.selected_gpu)
            state.error = False
            return
        _show_selected_reservation_details(stdscr, config, store, state)
        return
    if key in (ord("i"), ord("I")):
        info = administrator_info(config)
        if stdscr is None:
            state.message = administrator_display_lines(info)[0]
            state.error = False
        else:
            _message_dialog(
                stdscr,
                "GPUBK administrator",
                administrator_display_lines(info),
            )
        return
    if key in (ord("u"), ord("U")):
        if stdscr is None:
            state.message = "usage: sampled history for your UID; run bk u for CLI detail"
            state.error = False
        else:
            _show_usage_summary(stdscr, config)
        return
    if key in (ord("?"), ord("p"), ord("P")):
        if stdscr is None:
            state.message = "help: navigation, add/edit, timeline, and quick tour"
            state.error = False
        else:
            _help_dialog(stdscr)


def _pan_timeline(config: Config, state: TuiState, direction: int, multiplier: int = 1) -> None:
    step_slots = max(1, 60 // state.slot_minutes)
    retention_slots = max(0, config.ledger_retention_days * 24 * 60 // state.slot_minutes)
    earliest_offset = -max(0, retention_slots - NOW_CONTEXT_COLUMNS)
    latest_offset = max(0, config.queue_search_hours * 60 // state.slot_minutes)
    distance = step_slots * max(1, multiplier)
    requested = state.offset_slots + (distance if direction > 0 else -distance)
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
    state.zoom_index = min(
        len(state.zoom_levels) - 1,
        max(0, state.zoom_index + direction),
    )
    state.offset_slots = int(round(offset_minutes / state.slot_minutes))


def _cycle_speed(state: TuiState) -> None:
    state.speed_index = (state.speed_index + 1) % len(SPEED_LEVELS)
    state.message = f"adjustment speed {state.speed_multiplier}x"
    state.error = False


def _speed_zoom_step(state: TuiState) -> int:
    return SPEED_ZOOM_STEPS[state.speed_index]


def _change_editor_zoom(state: TuiState, direction: int) -> None:
    view_start = _editor_view_start(state)
    selection_start = view_start + timedelta(
        minutes=state.add_start_steps * state.booking_slot_minutes
    )
    selection_end = selection_start + timedelta(
        minutes=max(1, state.add_duration_steps) * state.booking_slot_minutes
    )
    _change_zoom(state, direction)
    _frame_editor_window(state, selection_start, selection_end, utc_now(), auto_zoom=False)


def _toggle_focus(config: Config, store: LedgerStore, state: TuiState) -> None:
    if state.focus == FOCUS_GPUS:
        state.focus = FOCUS_RESERVATIONS
        state.selected = -1
        state.message = "reservation header; Down selects a reservation"
    else:
        reservations = _active_reservations(store)
        if reservations and 0 <= state.selected < len(reservations):
            gpus = [
                int(gpu) for gpu in reservations[state.selected].get("gpus", [])
            ]
            if gpus:
                state.selected_gpu = min(max(gpus[0], 0), config.gpu_count - 1)
        state.focus = FOCUS_GPUS
        state.selected_gpu = min(max(state.selected_gpu, 0), config.gpu_count - 1)
        state.message = f"GPU {state.selected_gpu} focus"
    state.error = False


def _move_focus_up(config: Config, store: LedgerStore, state: TuiState, steps: int = 1) -> None:
    if state.focus == FOCUS_GPUS:
        state.selected_gpu = max(0, state.selected_gpu - max(1, steps))
        state.message = f"GPU {state.selected_gpu} focus"
        state.error = False
        return
    if state.selected >= 0:
        state.selected = max(-1, state.selected - max(1, steps))
        state.message = "reservation header" if state.selected < 0 else ""
        return
    state.focus = FOCUS_GPUS
    state.selected_gpu = max(0, config.gpu_count - 1)
    state.message = f"GPU {state.selected_gpu} focus"
    state.error = False


def _move_focus_down(config: Config, store: LedgerStore, state: TuiState, steps: int = 1) -> None:
    if state.focus == FOCUS_GPUS:
        if state.selected_gpu < config.gpu_count - 1:
            state.selected_gpu = min(config.gpu_count - 1, state.selected_gpu + max(1, steps))
            state.message = f"GPU {state.selected_gpu} focus"
        else:
            state.focus = FOCUS_RESERVATIONS
            state.selected = -1
            state.message = "reservation header; Down selects a reservation"
        state.error = False
        return
    reservations = _active_reservations(store)
    if reservations:
        state.selected = min(
            len(reservations) - 1, state.selected + max(1, steps)
        )
        state.message = ""


def _draw(stdscr, config: Config, store: LedgerStore, state: TuiState) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 18 or width < 72:
        _addstr(stdscr, 0, 0, "Terminal too small. Need at least 72x18.", width, COLOR_ERROR)
        stdscr.refresh()
        return

    now = utc_now()
    _refresh_collector_status(config, state, now)
    ledger = store.load()
    active_index = ReservationIndex.from_ledger(ledger, now)
    active = active_index.records()
    _refresh_worker_status(config, state, active, _current_actor(), now)
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
    usage_by_gpu = classify_process_usage(
        gpu_snapshots,
        active,
        now,
        container_groups=config.container_attribution_groups,
    )
    gpu_by_index = {gpu.index: gpu for gpu in gpu_snapshots}

    label_width = _timeline_label_width(width)
    timeline_width = max(24, width - label_width - 2)
    state.timeline_columns = timeline_width
    default_view_start = _timeline_view_start(now, state)
    view_start = state.editor_view_start if state.editor_active and state.editor_view_start else default_view_start
    view_end = view_start + timedelta(minutes=timeline_width * state.slot_minutes)
    visible_announcements = []
    if not state.editor_active:
        visible_announcements = [
            item
            for item in active_announcements(ledger, now=now)
            if item.get("level") in {"warning", "critical"}
        ]
    visible_blackouts = [
        item
        for item in config.booking_blackouts
        if parse_iso(item[0]) < view_end and view_start < parse_iso(item[1])
    ]
    banner_lines = _announcement_banner_lines(visible_announcements, width)
    if not banner_lines and not state.editor_active and visible_blackouts:
        blocked_start, blocked_end, reason = visible_blackouts[0]
        banner_lines = _bounded_banner_lines(
            f" BLACKOUT {format_local(blocked_start)} -> "
            f"{format_local(blocked_end)}: {reason}",
            width,
            max_lines=2,
            overflow_line="  ... run bk n for maintenance details",
        )
    timeline_top = 2 + len(banner_lines) if banner_lines else 3
    timeline_index = ReservationIndex.from_ledger(
        ledger,
        min(now, view_start),
        statuses=(STATUS_ACTIVE, STATUS_EXPIRED),
    )
    timeline_records = timeline_index.records()
    id_width = _visible_id_width([*active, *timeline_records])
    preview = _build_add_preview(ledger, config, state, view_start) if state.editor_active else None
    color_map = _reservation_color_map(timeline_records, timeline_index)

    visible_gpu_rows = max(1, height - (timeline_top + 4) - 8)
    gpu_anchor = _gpu_view_anchor(state, active, selected_id)
    gpu_anchor_position = next(
        (
            position
            for position, gpu in enumerate(gpu_snapshots)
            if gpu.index == gpu_anchor
        ),
        0,
    )
    gpu_view_start = _gpu_view_start(
        len(gpu_snapshots),
        visible_gpu_rows,
        gpu_anchor_position,
    )
    visible_gpu_snapshots = gpu_snapshots[
        gpu_view_start : gpu_view_start + visible_gpu_rows
    ]

    _draw_header(stdscr, config, now, view_start, view_end, width, state)
    _draw_editor_banner(stdscr, 2, width, state, preview, id_width)
    if not state.editor_active:
        if banner_lines:
            for offset, message in enumerate(banner_lines):
                _addstr(
                    stdscr,
                    2 + offset,
                    0,
                    pad_display_text(message, width),
                    width,
                    COLOR_PREVIEW_EXCLUSIVE,
                    curses.A_BOLD,
                )
    _draw_time_axis(stdscr, timeline_top, label_width, timeline_width, view_start, view_end, width, now)
    row = timeline_top + 4
    for gpu in visible_gpu_snapshots:
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
            state.timeline_style,
            gpu.index in config.disabled_gpus,
            config.booking_blackouts,
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
        id_width,
    )

    if focused_gpu is not None:
        gpu = gpu_by_index.get(focused_gpu, GpuSnapshot(index=focused_gpu, name="unknown"))
        _draw_gpu_focus_panel(
            stdscr,
            row,
            width,
            height,
            gpu,
            usage_by_gpu.get(focused_gpu, []),
            active,
            config.max_shared_users,
            id_width,
        )
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
            config.gpu_count,
            id_width,
        )
    _draw_footer(stdscr, height, width, state, preview)
    stdscr.refresh()


def _announcement_banner_lines(
    announcements: Sequence[dict],
    width: int,
    *,
    max_lines: int = 3,
) -> list[str]:
    if not announcements or width < 2 or max_lines < 1:
        return []
    if len(announcements) > 1:
        total = len(announcements)
        visible_limit = max_lines if total <= max_lines else max(1, max_lines - 1)
        lines = []
        for index, announcement in enumerate(announcements[:visible_limit], start=1):
            label = str(announcement.get("level", "warning")).upper()
            message = " ".join(
                str(announcement.get("message", "administrator announcement")).split()
            )
            lines.append(
                clip_display_text(
                    f" [{index}/{total}] {label}: {message}",
                    width - 1,
                )
            )
        if total > visible_limit:
            lines.append(f"  ... {total - visible_limit} more; run bk n for all announcements")
        return lines
    announcement = announcements[0]
    label = str(announcement.get("level", "warning")).upper()
    message = str(announcement.get("message", "administrator announcement"))
    return _bounded_banner_lines(
        f" {label}: {message}",
        width,
        max_lines=max_lines,
        overflow_line="  ... run bk n for full announcement",
    )


def _bounded_banner_lines(
    message: str,
    width: int,
    *,
    max_lines: int,
    overflow_line: str,
) -> list[str]:
    lines = wrap_display_text(
        message,
        width - 1,
        subsequent_indent="  ",
        preserve_newlines=False,
    )
    if len(lines) <= max_lines:
        return lines
    if max_lines == 1:
        return [overflow_line]
    return [*lines[: max_lines - 1], overflow_line]


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
        f" GPUBK | {window_mode} | now {local_now:%Y-%m-%d %H:%M:%S %z} "
        f"| window {local_start:%m-%d %H:%M} -> {local_end:%m-%d %H:%M} "
        f"| {state.slot_minutes}m/col "
    )
    wide_details = (
        f" monitor={_collector_status_text(state.collector_status)} "
        f"| worker={_worker_status_text(state.worker_status)} "
        f"| share={config.max_shared_users} slots/GPU "
        f"| GPUs={len(config.enabled_gpus)}/{config.gpu_count} enabled "
        f"| style={state.timeline_style} | refresh={config.tui_refresh_seconds:g}s"
    )
    if width >= 100 and len(wide_title) < width and len(wide_details) < width:
        title = wide_title
        details = wide_details
    else:
        title = (
            f" GPUBK {window_mode} | {_weekday_label(local_now)} {local_now:%m-%d %H:%M:%S} "
            f"| {local_start:%m-%d %H:%M}->{local_end:%m-%d %H:%M} | {state.slot_minutes}m"
        )
        suffix = (
            f" monitor={_collector_status_text(state.collector_status)} "
            f"| worker={_worker_status_text(state.worker_status)} "
            f"| {config.max_shared_users} slots | GPUs={len(config.enabled_gpus)}/{config.gpu_count} "
            f"| {state.timeline_style}"
        )
        details = suffix.lstrip()
    limit = max(0, width - 1)
    return title[:limit], details[:limit]


def _refresh_collector_status(config: Config, state: TuiState, now: datetime) -> None:
    if (
        state.collector_checked_at is not None
        and now < state.collector_checked_at + timedelta(seconds=10)
    ):
        return
    store = UsageAuditStore(
        config.data_dir,
        config.lock_timeout_seconds,
        config.file_mode,
        config.dir_mode,
        config.storage_gid,
    )
    state.collector_status = store.load_collector_status(
        now=now,
        expected_gpu_count=config.gpu_count,
    )
    state.collector_checked_at = now


def _collector_label(status: object) -> str:
    state = str(status.get("state", "unknown")) if isinstance(status, dict) else "unknown"
    return {
        "running": "OK",
        "degraded": "DEG",
        "stale": "STALE",
        "stopped": "STOP",
        "clock-skew": "CLOCK",
        "topology-mismatch": "TOPO",
        "not-seen": "--",
    }.get(state, "ERR")


def _collector_status_text(status: object) -> str:
    state = str(status.get("state", "unknown")) if isinstance(status, dict) else "unknown"
    return {
        "running": "ok",
        "degraded": "degraded",
        "stale": "stale",
        "stopped": "stopped",
        "clock-skew": "clock-skew",
        "topology-mismatch": "topology",
        "not-seen": "not-seen",
    }.get(state, "error")


def _refresh_worker_status(
    config: Config,
    state: TuiState,
    reservations: Sequence[dict],
    actor: Actor,
    now: datetime,
) -> None:
    if not reservations_need_worker(reservations, actor.uid):
        state.worker_status = {"state": "idle", "running": None}
        state.worker_checked_at = None
        return
    if (
        state.worker_checked_at is not None
        and now < state.worker_checked_at + timedelta(seconds=10)
    ):
        return
    state.worker_status = inspect_worker_status(config, actor, at=now)
    state.worker_checked_at = now


def _worker_label(status: object) -> str:
    if not isinstance(status, dict):
        return "ERR"
    state = str(status.get("state", "unknown"))
    if state == "running":
        return "OK" if status.get("running") is True else "ERR"
    return {
        "idle": "IDLE",
        "not-seen": "OFF",
        "stopped": "STOP",
        "other-instance": "OTHER",
        "unverified": "UNVER",
        "unavailable": "N/A",
    }.get(state, "ERR")


def _worker_status_text(status: object) -> str:
    if not isinstance(status, dict):
        return "error"
    state = str(status.get("state", "unknown"))
    if state == "running":
        return "running" if status.get("running") is True else "error"
    return {
        "idle": "idle",
        "not-seen": "off",
        "stopped": "stopped",
        "other-instance": "other-instance",
        "unverified": "unverified",
        "unavailable": "unavailable",
    }.get(state, "error")


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
    now_col = _timeline_now_col(now, start, end, timeline_width)
    now_label_col = None
    if now_col is not None:
        minutes, now_label_col = _clear_now_label_slot(minutes, now_col)
    _addstr(stdscr, row, 0, "Date".ljust(label_width), width, COLOR_MUTED)
    _addstr(stdscr, row + 1, 0, "Hour".ljust(label_width), width, COLOR_MUTED)
    _addstr(stdscr, row + 2, 0, "Minute".ljust(label_width), width, COLOR_MUTED)
    _addstr(stdscr, row + 3, 0, _gpu_metrics_header(label_width), width, COLOR_MUTED)
    _addstr(stdscr, row, label_width, dates, width, COLOR_MUTED)
    _addstr(stdscr, row + 1, label_width, hours, width, COLOR_MUTED)
    _addstr(stdscr, row + 2, label_width, minutes, width, COLOR_MUTED)
    _addstr(stdscr, row + 3, label_width, ruler, width, COLOR_MUTED)
    if now_col is not None and now_label_col is not None:
        _addstr(
            stdscr,
            row + 2,
            label_width + now_label_col,
            "NOW",
            width,
            COLOR_SELECTED,
            curses.A_BOLD,
        )
        _addstr(stdscr, row + 3, label_width + now_col, NOW_CHAR, width, COLOR_SELECTED, curses.A_BOLD)


def _draw_editor_banner(
    stdscr,
    row: int,
    width: int,
    state: TuiState,
    preview: Optional[AddPreview],
    id_width: int = MIN_SHORT_ID_WIDTH,
) -> None:
    if not state.editor_active or preview is None:
        return
    color = _preview_color(preview.mode) if preview.valid else COLOR_ERROR
    _addstr(
        stdscr,
        row,
        0,
        _editor_banner_text(state, preview, id_width),
        width,
        color,
        curses.A_BOLD,
    )


def _editor_banner_text(
    state: TuiState,
    preview: AddPreview,
    id_width: int = MIN_SHORT_ID_WIDTH,
) -> str:
    operation = "EDIT" if state.edit_mode else "ADD"
    if state.edit_mode and state.edit_reservation_id:
        operation += f" {state.edit_reservation_id[:id_width]}"
    if state.edit_started:
        operation += " END-ONLY"
    mode = (
        f"S{preview.share_units}"
        if preview.mode == MODE_SHARED
        else "X"
    )
    gpu_text = ",".join(map(str, preview.selected_gpus)) or "-"
    local_start = preview.start.astimezone()
    local_end = preview.end.astimezone()
    status = "READY" if preview.valid else "BLOCKED"
    memory = _editor_memory_label(state)
    return (
        f" {operation} {mode} | {len(preview.selected_gpus)} GPU [{gpu_text}] | "
        f"{_weekday_label(local_start)} {local_start:%m-%d %H:%M}->{local_end:%H:%M} | "
        f"{_duration_detail_text(local_end - local_start)} | slice {state.booking_slot_minutes}m | "
        f"{state.speed_multiplier}x | {memory} | {status} "
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
    timeline_style: str = "capacity",
    disabled: bool = False,
    booking_blackouts: Sequence[Tuple[str, str, str]] = (),
) -> None:
    view_end = start + timedelta(minutes=slot_minutes * timeline_width)
    if now is not None:
        peak_shared = shared_capacity_units_for_gpu(
            active,
            gpu.index,
            now,
            now + timedelta(seconds=1),
            shared_limit,
        )
        exclusive_now = _gpu_is_exclusive_now(
            active,
            gpu.index,
            now,
            reservation_index,
        )
    else:
        peak_shared = _peak_shared_count_for_gpu(
            active,
            gpu.index,
            start,
            view_end,
            reservation_index,
            shared_limit,
        )
        exclusive_now = False
    violations = sum(1 for item in process_usage if item.violation)
    label = _gpu_row_label(
        gpu,
        label_width,
        peak_shared,
        shared_limit,
        violations,
        focused,
        exclusive_now,
        disabled,
    )
    cursor_active = preview is not None and gpu.index == preview.cursor_gpu
    if cursor_active:
        label_color = _preview_color(preview.mode)
    elif disabled or violations:
        label_color = COLOR_ERROR
    else:
        label_color = COLOR_SELECTED if focused else COLOR_MUTED
    label_attr = curses.A_BOLD if cursor_active or focused else 0
    for lane in range(max(1, band_rows)):
        row_label = label if lane == 0 else ""
        _addstr(stdscr, row + lane, 0, row_label[:label_width].ljust(label_width), width, label_color, label_attr)
        for col in range(timeline_width):
            left = start + timedelta(minutes=slot_minutes * col)
            right = left + timedelta(minutes=slot_minutes)
            preview_cell = _preview_cell_for_gpu(gpu.index, left, right, preview, lane, band_rows)
            if preview_cell is not None:
                char, color, attr = preview_cell
            elif _blackout_overlaps(booking_blackouts, left, right):
                char, color, attr = "▒", COLOR_PREVIEW_EXCLUSIVE, curses.A_BOLD
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
                    shared_limit,
                    timeline_style,
                )
            char, color, attr = _decorate_timeline_cell(char, color, attr, left, right, now)
            _addstr(stdscr, row + lane, label_width + col, char, width, color, attr)


def _blackout_overlaps(
    blackouts: Sequence[Tuple[str, str, str]],
    start: datetime,
    end: datetime,
) -> bool:
    return any(
        parse_iso(blocked_start) < end and start < parse_iso(blocked_end)
        for blocked_start, blocked_end, _reason in blackouts
    )


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
    id_width: int = MIN_SHORT_ID_WIDTH,
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
        label = _share_lane_label(gpu, reservation, shared_limit, hidden, id_width)
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


def _draw_gpu_focus_panel(
    stdscr,
    top: int,
    width: int,
    height: int,
    gpu: GpuSnapshot,
    usage: Sequence[ProcessUsage],
    reservations: Sequence[dict],
    shared_limit: int,
    id_width: int = MIN_SHORT_ID_WIDTH,
) -> None:
    if top >= height - 2:
        return
    related = [item for item in reservations if gpu.index in item.get("gpus", [])]
    util = f"{gpu.utilization_percent}%" if gpu.utilization_percent is not None else "n/a"
    if gpu.memory_total_mb:
        memory = f"{gpu.memory_used_mb / 1024:.1f}/{gpu.memory_total_mb / 1024:.0f}G"
    else:
        memory = "n/a"
    violations = sum(1 for item in usage if item.violation)
    title = (
        f"GPU {gpu.index} details | util={util} mem={memory} "
        f"| reservations={len(related)} processes={len(usage)} | Enter expands"
    )
    if violations:
        title += f" | violations={violations}"
    _addstr(stdscr, top, 0, title.ljust(width), width, COLOR_HEADER)
    row = top + 1
    if related:
        if row >= height - 2:
            return
        _addstr(stdscr, row, 0, "Reservations", width, COLOR_MUTED, curses.A_BOLD)
        row += 1
        if row >= height - 2:
            return
        _addstr(
            stdscr,
            row,
            0,
            _gpu_booking_header(width, id_width),
            width,
            COLOR_MUTED,
        )
        row += 1
        remaining = max(0, height - row - 4)
        booking_rows = min(4, len(related), remaining)
        for number, reservation in enumerate(related[:booking_rows], start=1):
            _addstr(
                stdscr,
                row,
                0,
                _gpu_booking_line(
                    reservation,
                    width,
                    shared_limit,
                    id_width,
                    number=number,
                ),
                width,
                _reservation_color(reservation),
            )
            row += 1
        if len(related) > booking_rows and row < height - 2:
            _addstr(stdscr, row, 2, f"+{len(related) - booking_rows} more (Enter expands)", width - 2, COLOR_MUTED)
            row += 1
    if row >= height - 2:
        return
    _addstr(stdscr, row, 0, "Processes", width, COLOR_MUTED, curses.A_BOLD)
    row += 1
    if row >= height - 2:
        return
    _addstr(stdscr, row, 0, _process_table_header(width, id_width), width, COLOR_MUTED)
    row += 1
    process_rows = min(6, len(usage), max(0, height - row - 2))
    for item in usage[:process_rows]:
        color = COLOR_ERROR if item.violation else (COLOR_MUTED if item.status in {"unknown", "system"} else COLOR_MINE)
        attr = curses.A_BOLD if item.violation else 0
        _addstr(
            stdscr,
            row,
            0,
            _process_table_line(item, width, id_width),
            width,
            color,
            attr,
        )
        row += 1
    if len(usage) > process_rows and row < height - 2:
        _addstr(stdscr, row, 2, f"+{len(usage) - process_rows} more (Enter expands)", width - 2, COLOR_MUTED)


def _gpu_booking_header(width: int, id_width: int) -> str:
    if width < 100:
        header = (
            f"{'#':>3} {'ID':<{id_width}} {'User':<10} {'M':<1} {'Req':<5} "
            f"{'VRAM':<7} {'Job':<10} {'Start':<11} {'End':<11} {'Dur':<5}"
        )
    else:
        header = (
            f"{'#':>3} {'ID':<{id_width}} {'User':<12} {'Mode':<4} {'Req':<5} "
            f"{'VRAM/GPU':<9} {'Job':<16} {'Start':<11} {'End':<11} {'Dur':<7}"
        )
    return header[: max(0, width - 1)]


def _gpu_booking_line(
    reservation: dict,
    width: int,
    shared_limit: int,
    id_width: int,
    *,
    number: int = 1,
) -> str:
    start = parse_iso(reservation["start_at"]).astimezone()
    end = parse_iso(reservation["end_at"]).astimezone()
    request = _capacity_text(reservation, (), shared_limit)
    memory = _reservation_memory_text(reservation)
    job = _reservation_job_text(reservation)
    duration = _duration_text(end - start)
    if width < 100:
        line = (
            f"{number:>3} {str(reservation.get('id', ''))[:id_width]:<{id_width}} "
            f"{_truncate(str(reservation.get('username', '?')), 10):<10} "
            f"{_mode_mark(reservation):<1} {request:<5} "
            f"{_truncate(memory, 7):<7} {_truncate(job, 10):<10} "
            f"{start:%m-%d %H:%M} {end:%m-%d %H:%M} {_truncate(duration, 5):<5}"
        )
    else:
        line = (
            f"{number:>3} {str(reservation.get('id', ''))[:id_width]:<{id_width}} "
            f"{_truncate(str(reservation.get('username', '?')), 12):<12} "
            f"{str(reservation.get('mode', '?'))[:4]:<4} {request:<5} "
            f"{_truncate(memory, 9):<9} {_truncate(job, 16):<16} "
            f"{start:%m-%d %H:%M} {end:%m-%d %H:%M} {_truncate(duration, 7):<7}"
        )
    return line[: max(0, width - 1)]


def _reservation_memory_text(reservation: dict) -> str:
    if reservation.get("mode") == MODE_EXCLUSIVE:
        return "-"
    value = reservation.get("expected_memory_mb")
    return _memory_compact(int(value)) if isinstance(value, int) and value > 0 else "auto"


def _reservation_job_text(reservation: dict) -> str:
    job = reservation.get("job")
    if not isinstance(job, dict):
        return "-"
    summary = str(job.get("summary", "")).strip()
    return summary or str(job.get("status", "job"))


def _process_table_header(width: int, id_width: int = MIN_SHORT_ID_WIDTH) -> str:
    if width < 100:
        return (
            f"{'PID':>7} {'User':<10} {'T':<3} {'SM':>3} {'Mem':>6} "
            f"{'State':<11} {'Res ID':<{id_width}} Process"
        )
    return (
        f"{'PID':>7} {'User':<16} {'Type':<4} {'SM':>4} {'Memory':>8} "
        f"{'State':<11} {'Res ID':<{id_width}} Process"
    )


def _process_table_line(
    item: ProcessUsage,
    width: int,
    id_width: int = MIN_SHORT_ID_WIDTH,
) -> str:
    process = item.process
    owner = process_owner_label(process)
    sm = f"{process.sm_utilization_percent}%" if process.sm_utilization_percent is not None else "-"
    memory = f"{process.gpu_memory_mb}M" if process.gpu_memory_mb else "-"
    booking = _truncate(",".join(value[:id_width] for value in item.reservation_ids) or "-", id_width)
    command = summarize_process_command(process.command)
    container = process_container_label(process)
    if container:
        command = f"{container} {command}"
    if width < 100:
        prefix = (
            f"{process.pid:>7} {_truncate(owner, 10):<10} "
            f"{process.kind:<3} {sm:>3} {memory:>6} {item.status:<11} "
            f"{booking:<{id_width}} "
        )
    else:
        prefix = (
            f"{process.pid:>7} {_truncate(owner, 16):<16} "
            f"{process.kind:<4} {sm:>4} {memory:>8} {item.status:<11} "
            f"{booking:<{id_width}} "
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
    shared_limit: int = 1,
    shared_style: str = "capacity",
) -> Tuple[str, int, int]:
    if reservation_index is None:
        overlapping_spans = sorted(
            [
                (
                    item,
                    parse_iso(item["start_at"]),
                    parse_iso(item["end_at"]),
                )
                for item in active
                if gpu in item.get("gpus", [])
                and parse_iso(item["start_at"]) < end
                and start < parse_iso(item["end_at"])
            ],
            key=lambda item: (item[1], item[2], str(item[0].get("id", ""))),
        )
    else:
        overlapping_spans = [
            (item.record, item.start, item.end)
            for item in reservation_index.overlapping(gpu, start, end)
        ]
    if not overlapping_spans:
        return FREE_CHAR, COLOR_FREE, 0
    overlapping = [item[0] for item in overlapping_spans]
    edge_cell = _reservation_edge_cell(
        overlapping_spans,
        start,
        end,
        color_map,
        selected_id,
    )
    if edge_cell is not None:
        return edge_cell
    exclusive = _choose_selected_or_first([item for item in overlapping if item.get("mode") == MODE_EXCLUSIVE], selected_id)
    if exclusive is not None:
        if selected_id and exclusive.get("id") == selected_id:
            return BAR_CHAR, _reservation_color(exclusive, color_map), _selected_bar_attr()
        return BAR_CHAR, _reservation_color(exclusive, color_map), curses.A_BOLD
    shared_items = [item for item in overlapping if item.get("mode") == MODE_SHARED]
    chosen = _choose_selected_or_first(shared_items, selected_id)
    if chosen is None:
        return FREE_CHAR, COLOR_FREE, 0
    visual_slots = _shared_visual_slots(shared_items, shared_limit)
    occupied_slots = [item for item in visual_slots if item is not None]
    if lane_count <= 1:
        if shared_style == "solid" and len(shared_items) == 1:
            attr = curses.A_BOLD
            if selected_id and chosen.get("id") == selected_id:
                attr |= curses.A_BLINK
            return BAR_CHAR, _reservation_color(chosen, color_map), attr
        pair_source = (
            occupied_slots
            if shared_style == "solid" and occupied_slots
            else visual_slots
        )
        top_item, bottom_item = _shared_visual_pair(pair_source, col)
        return _render_shared_pair(top_item, bottom_item, color_map, selected_id, col)

    visible = _shared_lane_item(occupied_slots, selected_id, col, lane, lane_count)
    attr = curses.A_BOLD
    if selected_id and visible.get("id") == selected_id:
        attr |= curses.A_BLINK
    char = SHARED_CHAR if lane_count <= 1 else BAR_CHAR
    return char, _reservation_color(visible, color_map), attr


def _reservation_edge_cell(
    spans: Sequence[Tuple[dict, datetime, datetime]],
    cell_start: datetime,
    cell_end: datetime,
    color_map: dict[str, int],
    selected_id: Optional[str],
) -> Optional[Tuple[str, int, int]]:
    """Keep reservation boundaries visible at every timeline zoom level."""
    # Shared overlaps need the whole cell for their split/weave encoding. Their
    # individual boundaries remain available in the expanded GPU lanes.
    if len(spans) != 1:
        return None
    edges = []
    for reservation, reservation_start, reservation_end in spans:
        starts = cell_start <= reservation_start < cell_end
        ends = cell_start < reservation_end <= cell_end
        if starts or ends:
            edges.append((reservation, starts, ends))
    if not edges:
        return None
    selected = next(
        (item for item in edges if selected_id and item[0].get("id") == selected_id),
        None,
    )
    reservation, starts, ends = selected or edges[0]
    if starts and ends:
        char = SHARED_SHORT_CHAR
    elif starts:
        char = SHARED_START_CHAR
    else:
        char = SHARED_END_CHAR
    attr = curses.A_BOLD
    if selected_id and reservation.get("id") == selected_id:
        attr |= curses.A_BLINK
    return char, _reservation_color(reservation, color_map), attr


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
    gpu_count: int,
    id_width: int = MIN_SHORT_ID_WIDTH,
) -> None:
    if not active:
        state.selected = -1
    elif state.selected >= len(active):
        state.selected = len(active) - 1

    rows = max(1, height - top - 4)
    view_start = _reservation_view_start(len(active), rows, state.selected)
    view_end = min(len(active), view_start + rows)
    header_focused = state.focus == FOCUS_RESERVATIONS and state.selected < 0 and not state.editor_active
    title = "> Reservations" if header_focused else "  Reservations"
    if len(active) > rows:
        title += f" {view_start + 1}-{view_end}/{len(active)}"
    _addstr(stdscr, top, 0, title.ljust(width), width, COLOR_HEADER, curses.A_BOLD if header_focused else 0)
    header = _table_header(width, gpu_count, id_width)
    _addstr(stdscr, top + 1, 0, header, width, COLOR_MUTED)
    if not active:
        _addstr(stdscr, top + 2, 0, "  none", width, COLOR_MUTED)
        return

    for offset, reservation in enumerate(active[view_start:view_end]):
        row = top + 2 + offset
        number = view_start + offset + 1
        prefix = f">{number}" if reservation.get("id") == selected_id else str(number)
        line = _reservation_table_line(
            reservation,
            prefix,
            width,
            active,
            shared_limit,
            gpu_count,
            id_width,
        )
        selected = reservation.get("id") == selected_id
        color = _reservation_color(reservation, color_map)
        _addstr(stdscr, row, 0, line, width, color, curses.A_BOLD if selected else 0)
        _draw_reservation_gpu_cells(
            stdscr,
            row,
            width,
            reservation,
            gpu_count,
            id_width,
            color,
            selected,
        )


def _draw_reservation_gpu_cells(
    stdscr,
    row: int,
    width: int,
    reservation: dict,
    gpu_count: int,
    id_width: int,
    reservation_color: int,
    selected: bool,
) -> None:
    if gpu_count > GPU_POSITION_MAP_MAX:
        return
    user_width = 10 if width < 100 else 16
    mode_width = 1 if width < 100 else 4
    start_col = 7 + id_width + user_width + mode_width
    gpu_text = _reservation_gpu_text(reservation.get("gpus", []), gpu_count, gpu_count)
    _addstr(stdscr, row, start_col, gpu_text, width, COLOR_MUTED)
    for gpu in reservation.get("gpus", []):
        if isinstance(gpu, int) and 0 <= gpu < gpu_count:
            _addstr(
                stdscr,
                row,
                start_col + gpu,
                str(gpu),
                width,
                reservation_color,
                curses.A_BOLD if selected else 0,
            )


def _reservation_view_start(total: int, rows: int, selected: int) -> int:
    visible_rows = max(1, rows)
    if total <= visible_rows or selected < visible_rows:
        return 0
    return min(max(0, total - visible_rows), selected - visible_rows + 1)


def _gpu_view_start(total: int, rows: int, anchor: int) -> int:
    visible_rows = max(1, rows)
    if total <= visible_rows:
        return 0
    clamped_anchor = min(max(0, anchor), total - 1)
    if clamped_anchor < visible_rows:
        return 0
    return min(max(0, total - visible_rows), clamped_anchor - visible_rows + 1)


def _gpu_view_anchor(
    state: TuiState,
    reservations: Sequence[dict],
    selected_id: Optional[str],
) -> int:
    if state.editor_active:
        return state.add_cursor_gpu
    if state.focus == FOCUS_GPUS:
        return state.selected_gpu
    selected = _reservation_by_id(reservations, selected_id)
    if selected is not None and selected.get("gpus"):
        return int(selected["gpus"][0])
    return 0


def _draw_footer(stdscr, height: int, width: int, state: TuiState, preview: Optional[AddPreview]) -> None:
    if state.editor_active and preview is not None:
        operation = "edit" if state.edit_mode else "add"
        message = state.message or _preview_status_text(preview, operation)
        message_color = COLOR_ERROR if state.error or not preview.valid else _preview_color(preview.mode)
    elif state.focus == FOCUS_GPUS:
        message = state.message or f"GPU {state.selected_gpu} focus"
        message_color = COLOR_ERROR if state.error else COLOR_SELECTED
    else:
        message = state.message or ("reservation header; Down selects a reservation" if state.selected < 0 else "")
        message_color = COLOR_ERROR if state.error else COLOR_MUTED
    footer = _footer_label(state, preview, width)
    _addstr(stdscr, height - 2, 0, message[: width - 1], width, message_color)
    _addstr(stdscr, height - 1, 0, footer.ljust(width), width, COLOR_HEADER)


def _footer_label(state: TuiState, preview: Optional[AddPreview], width: int) -> str:
    if state.editor_active and preview is not None:
        operation = "ADD" if state.add_mode else "EDIT"
        action = "book" if state.add_mode else "save"
        medium = (
            f" {operation} {state.speed_multiplier}x | arrows | [] dur | Space GPU | s/x "
            f"| f first | Enter {action} | Esc | ?"
        )
        short = (
            f" {operation} {state.speed_multiplier}x | arrows | [] dur | f first "
            f"| Enter {action} | Esc cancel | ?"
        )
        compact = f" {operation} | arrows | f first | Enter | Esc | ?"
        long = (
            f" {operation} {state.speed_multiplier}x | arrows move | Space GPU | [] duration | ,. quick "
            "| -/= zoom | Shift faster | v speed | m memory | u share | s/x "
            "| 1-9/f earliest | o nearest | g fixed "
            "| r reset | Enter/Esc | ? help "
        )
        return _first_fitting_footer((long, medium, short, compact), width)
    elif state.focus == FOCUS_GPUS:
        short = " GPU | arrows | Enter details | Tab RSV | a add | u usage | n NOW | ? | q quit"
        compact = " GPU | arrows | Enter | Tab RSV | a add | u | n NOW | ? | q quit"
        long = (
            " GPU FOCUS | up/down select GPU | Tab reservations | a add here "
            "| Enter details | -/= zoom | <-/-> history/future | Shift faster "
            "| v speed | z style | u usage | n NOW | r refresh | i admin | ? help | q quit "
        )
    else:
        short = " RSV | arrows | Enter details | Tab GPU | a/e/d | u usage | n NOW | ? | q quit"
        compact = " RSV | arrows | Enter | Tab GPU | a/e/d | u | n NOW | ? | q quit"
        long = (
            " RESERVATIONS | up/down select | Enter details | Tab GPUs | a add | e edit | d delete "
            f"| -/= zoom {state.slot_minutes}m/col | <-/-> history/future | Shift faster "
            "| v speed | z style | u usage | n NOW | r refresh | i admin | ? help | q quit "
        )
    return _first_fitting_footer((long, short, compact), width)


def _first_fitting_footer(candidates: Sequence[str], width: int) -> str:
    available = max(0, width - 1)
    for candidate in candidates:
        if len(candidate) <= available:
            return candidate
    return candidates[-1][:available]


def _nearest_enabled_gpu(config: Config, preferred: int) -> int:
    if not config.enabled_gpus:
        return 0
    return min(config.enabled_gpus, key=lambda gpu: (abs(gpu - preferred), gpu))


def _start_add_select(config: Config, state: TuiState) -> None:
    now = utc_now()
    previous_slot = max(1, state.booking_slot_minutes)
    duration_minutes = max(1, state.add_duration_steps) * previous_slot
    _configure_booking_slot(state, config.slot_minutes)
    state.add_duration_steps = max(
        1,
        (duration_minutes + config.slot_minutes - 1) // config.slot_minutes,
    )
    view_start = _default_timeline_view_start(
        now, state.slot_minutes, state.booking_slot_minutes
    )
    booking_start = _floor_to_add_step(now, state.booking_slot_minutes)
    state.add_mode = True
    state.edit_mode = False
    state.edit_reservation_id = None
    state.editor_view_start = view_start
    state.add_search_anchor = booking_start
    state.add_cursor_gpu = _nearest_enabled_gpu(config, state.add_cursor_gpu)
    state.add_start_steps = max(
        0,
        int(
            (booking_start - view_start).total_seconds()
            // (state.booking_slot_minutes * 60)
        ),
    )
    state.add_duration_steps = max(1, state.add_duration_steps)
    state.add_selected_gpus = (
        {state.add_cursor_gpu} if state.add_cursor_gpu in config.enabled_gpus else set()
    )
    state.add_booking_mode = MODE_SHARED
    state.add_expected_memory_mb = None
    state.add_share_units = 1
    if config.enabled_gpus:
        state.message = "Add: 1-9/f finds earliest from NOW; arrows set a new search start"
        state.error = False
    else:
        state.message = "all GPUs are disabled by the administrator"
        state.error = True


def _start_edit_select(config: Config, store: LedgerStore, state: TuiState) -> None:
    reservations = _active_reservations(store)
    if not reservations:
        state.message = "no reservation to edit"
        state.error = True
        return
    if state.selected < 0:
        state.message = "select a reservation before editing"
        state.error = True
        return
    state.selected = min(state.selected, len(reservations) - 1)
    reservation = reservations[state.selected]
    if int(reservation.get("uid", -1)) != _current_actor().uid:
        state.message = "you can inspect this reservation, but only its owner can edit it"
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
    state.edit_started = start <= utc_now()
    _configure_booking_slot(state, config.slot_minutes)
    state.edit_reservation_id = str(reservation["id"])
    context_steps = max(1, (30 + state.booking_slot_minutes - 1) // state.booking_slot_minutes)
    state.editor_view_start = start - timedelta(
        minutes=context_steps * state.booking_slot_minutes
    )
    state.add_start_steps = context_steps
    state.add_search_anchor = start
    state.add_duration_steps = max(
        1,
        int((end - start).total_seconds() // (state.booking_slot_minutes * 60)),
    )
    state.add_selected_gpus = gpus
    state.add_cursor_gpu = min(gpus) if gpus else 0
    state.add_booking_mode = reservation.get("mode") if reservation.get("mode") in {MODE_SHARED, MODE_EXCLUSIVE} else MODE_SHARED
    raw_memory = reservation.get("expected_memory_mb")
    state.add_expected_memory_mb = int(raw_memory) if raw_memory is not None else None
    state.add_share_units = reservation_share_units(reservation, config.max_shared_users)
    state.message = (
        "running reservation: resources and elapsed time locked; adjust end with [ ]"
        if state.edit_started
        else ""
    )
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
            state.message = "help: f earliest, o nearest, g fixed GPUs, r reset, Enter submit"
            state.error = False
        else:
            _help_dialog(stdscr, initial_page=1)
        return
    if key in (27, ord("q"), ord("Q")):
        _close_editor(state)
        state.message = f"{operation} cancelled"
        state.error = False
        return
    if key in (ord("v"), ord("V")):
        _cycle_speed(state)
        return
    if state.edit_started and key not in {
        ord("["), ord("]"), ord("{"), ord("}"), ord(","), ord("."),
        ord("<"), ord(">"), ord("+"), ord("="), ord("_"), ord("-"),
        ord("r"), ord("R"), curses.KEY_ENTER, 10, 13,
    }:
        state.message = "running edit is end-only: [ ] adjust, v accelerates, Enter applies"
        state.error = True
        return
    if key in (KEY_SHIFT_RIGHT, ord("L")):
        _move_editor_start(state, state.speed_multiplier * _quick_duration_steps(state))
        _clear_editor_feedback(state)
        return
    if key in (KEY_SHIFT_LEFT, ord("H")):
        _move_editor_start(state, -state.speed_multiplier * _quick_duration_steps(state))
        _clear_editor_feedback(state)
        return
    if key in (curses.KEY_RIGHT, ord("l")):
        _move_editor_start(state, state.speed_multiplier)
        _clear_editor_feedback(state)
        return
    if key in (curses.KEY_LEFT, ord("h")):
        _move_editor_start(state, -state.speed_multiplier)
        _clear_editor_feedback(state)
        return
    if key in (KEY_SHIFT_DOWN, ord("J")):
        distance = state.speed_multiplier * ACCELERATED_GPU_ROWS
        state.add_cursor_gpu = min(max(0, config.gpu_count - 1), state.add_cursor_gpu + distance)
        _clear_editor_feedback(state)
        return
    if key in (KEY_SHIFT_UP, ord("K")):
        state.add_cursor_gpu = max(0, state.add_cursor_gpu - state.speed_multiplier * ACCELERATED_GPU_ROWS)
        _clear_editor_feedback(state)
        return
    if key in (curses.KEY_DOWN, ord("j")):
        state.add_cursor_gpu = min(max(0, config.gpu_count - 1), state.add_cursor_gpu + state.speed_multiplier)
        _clear_editor_feedback(state)
        return
    if key in (curses.KEY_UP, ord("k")):
        state.add_cursor_gpu = max(0, state.add_cursor_gpu - state.speed_multiplier)
        _clear_editor_feedback(state)
        return
    if key == ord(" "):
        if state.add_cursor_gpu in state.add_selected_gpus:
            state.add_selected_gpus.remove(state.add_cursor_gpu)
        elif state.add_cursor_gpu in config.disabled_gpus:
            state.message = f"GPU {state.add_cursor_gpu} disabled by the administrator"
            state.error = True
            return
        elif 0 <= state.add_cursor_gpu < config.gpu_count:
            state.add_selected_gpus.add(state.add_cursor_gpu)
        _clear_editor_feedback(state)
        return
    if key == ord("["):
        state.add_duration_steps = max(1, state.add_duration_steps - state.speed_multiplier)
        _clear_editor_feedback(state)
        return
    if key == ord("]"):
        state.add_duration_steps += state.speed_multiplier
        _clear_editor_feedback(state)
        return
    if key == ord("{"):
        state.add_duration_steps = max(
            1,
            state.add_duration_steps - state.speed_multiplier * _quick_duration_steps(state),
        )
        _clear_editor_feedback(state)
        return
    if key == ord("}"):
        state.add_duration_steps += state.speed_multiplier * _quick_duration_steps(state)
        _clear_editor_feedback(state)
        return
    if key == ord(","):
        step = state.speed_multiplier * _quick_duration_steps(state)
        state.add_duration_steps = max(1, state.add_duration_steps - step)
        state.message = f"duration {_editor_duration_text(state)}"
        state.error = False
        return
    if key == ord("."):
        step = state.speed_multiplier * _quick_duration_steps(state)
        state.add_duration_steps += step
        state.message = f"duration {_editor_duration_text(state)}"
        state.error = False
        return
    if key == ord("<"):
        step = state.speed_multiplier * _quick_duration_steps(state) * ACCELERATED_MULTIPLIER
        state.add_duration_steps = max(1, state.add_duration_steps - step)
        state.message = f"fast duration {_editor_duration_text(state)}"
        state.error = False
        return
    if key == ord(">"):
        step = state.speed_multiplier * _quick_duration_steps(state) * ACCELERATED_MULTIPLIER
        state.add_duration_steps += step
        state.message = f"fast duration {_editor_duration_text(state)}"
        state.error = False
        return
    if key == ord("+"):
        _change_editor_zoom(state, -_speed_zoom_step(state) * ACCELERATED_ZOOM_LEVELS)
        state.message = f"fast zoom {state.slot_minutes}m/col; quick step {_quick_duration_text(state)}"
        state.error = False
        return
    if key == ord("="):
        _change_editor_zoom(state, -_speed_zoom_step(state))
        state.message = f"zoom {state.slot_minutes}m/col; quick step {_quick_duration_text(state)}"
        state.error = False
        return
    if key == ord("_"):
        _change_editor_zoom(state, _speed_zoom_step(state) * ACCELERATED_ZOOM_LEVELS)
        state.message = f"fast zoom {state.slot_minutes}m/col; quick step {_quick_duration_text(state)}"
        state.error = False
        return
    if key == ord("-"):
        _change_editor_zoom(state, _speed_zoom_step(state))
        state.message = f"zoom {state.slot_minutes}m/col; quick step {_quick_duration_text(state)}"
        state.error = False
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
        raw = _prompt_line(
            stdscr,
            "Memory per GPU (12g/4096m, - clears)",
            default,
            title="Expected VRAM",
        )
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
    if key == ord("u"):
        if stdscr is None:
            state.message = "press u in the live TUI to set shared capacity"
            state.error = False
            return
        if state.add_booking_mode == MODE_EXCLUSIVE:
            state.message = "share capacity applies only to shared reservations"
            state.error = True
            return
        default = str(state.add_share_units)
        usage = _editor_shared_slot_usage(config, store, state)
        raw = _prompt_line(
            stdscr,
            f"Slots/GPU max={config.max_shared_users} {_editor_slot_usage_text(usage)} request=",
            default,
            title="Shared slots",
        )
        if raw:
            try:
                state.add_share_units = parse_share_units(raw, config.max_shared_users)
            except ValueError as exc:
                state.message = str(exc)
                state.error = True
                return
        state.message = (
            f"shared request: {state.add_share_units} slot(s)/GPU; "
            f"maximum {config.max_shared_users}"
        )
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
    if key == ord("o"):
        _find_add_slot(config, store, state, fixed_gpus=False, nearest=True)
        return
    if key == ord("g"):
        _find_add_slot(config, store, state, fixed_gpus=True)
        return
    if key in (curses.KEY_ENTER, 10, 13):
        ledger = store.load()
        if state.edit_mode and not state.edit_started:
            current = next(
                (
                    item
                    for item in ledger.get("reservations", [])
                    if item.get("id") == state.edit_reservation_id
                ),
                None,
            )
            if current is not None and parse_iso(current["start_at"]) <= utc_now():
                _load_edit_state(config, state, current)
                state.message = (
                    "reservation started while editing; switched to end-only mode"
                )
                state.error = False
                return
        view_start = _editor_view_start(state)
        preview = _build_add_preview(ledger, config, state, view_start)
        if not preview.valid:
            state.message = preview.reason
            state.error = True
            return
        try:
            if state.edit_mode:
                if not state.edit_reservation_id:
                    raise BookingError("reservation not found")
                if state.edit_started:
                    request = EditRequest(
                        actor=_current_actor(),
                        reservation_id=state.edit_reservation_id,
                        duration_seconds=int((preview.end - preview.start).total_seconds()),
                        gpu_memory_capacity_mb=state.gpu_memory_capacity_mb,
                    )
                else:
                    request = EditRequest(
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
                        share_units=state.add_share_units if preview.mode == MODE_SHARED else None,
                        update_share_units=True,
                    )
                result = edit_booking(store, config, request)
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
                        share_units=state.add_share_units if preview.mode == MODE_SHARED else None,
                    ),
                )
        except BookingError as exc:
            state.message = str(exc)
            state.error = True
            return
        reservation_id = str(result.reservation["id"])
        visible_after = list_active(store.load())
        id_width = _visible_id_width(visible_after)
        _close_editor(state)
        state.focus = FOCUS_RESERVATIONS
        state.selected = _own_reservation_index(store, reservation_id)
        state.message = (
            f"{'updated' if operation == 'edit' else 'created'} "
            f"{reservation_id[:id_width]}"
        )
        state.error = False


def _find_add_slot(
    config: Config,
    store: LedgerStore,
    state: TuiState,
    fixed_gpus: bool,
    requested_count: Optional[int] = None,
    *,
    nearest: bool = False,
) -> None:
    selected = sorted(gpu for gpu in state.add_selected_gpus if 0 <= gpu < config.gpu_count)
    if fixed_gpus and not selected:
        state.message = "select at least one GPU before fixed search"
        state.error = True
        return

    disabled = sorted(set(selected) & set(config.disabled_gpus))
    if fixed_gpus and disabled:
        state.message = (
            f"GPU {','.join(map(str, disabled))} disabled by the administrator"
        )
        state.error = True
        return

    count = requested_count if requested_count is not None else (len(selected) or 1)
    maximum = config.gpu_count if fixed_gpus else len(config.enabled_gpus)
    if count < 1 or count > maximum:
        state.message = f"GPU count must be between 1 and {maximum} enabled GPU(s)"
        state.error = True
        return
    view_start = _editor_view_start(state)
    selected_start = view_start + timedelta(
        minutes=state.add_start_steps * state.booking_slot_minutes
    )
    now = utc_now()
    minimum_start = (
        _ceil_to_add_step(now, state.booking_slot_minutes)
        if state.edit_mode
        else _floor_to_add_step(now, state.booking_slot_minutes)
    )
    search_anchor = state.add_search_anchor or selected_start
    search_start = max(selected_start if nearest else search_anchor, minimum_start)
    duration = timedelta(
        minutes=max(1, state.add_duration_steps) * state.booking_slot_minutes
    )
    mode = state.add_booking_mode if state.add_booking_mode in {MODE_SHARED, MODE_EXCLUSIVE} else MODE_SHARED
    ledger = _availability_ledger(store.load(), state, at=now)
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
            start_at=search_start,
            mode=mode,
            expected_memory_mb=state.add_expected_memory_mb,
            share_units=state.add_share_units,
        )
        if not fixed_gpus
        else AllocatorDecision(list(advice.order), dict(advice.scores), "fixed-gpu")
    )
    search_arguments = {
        "preferred_gpus": selected if fixed_gpus else None,
        "gpu_order": allocator.order,
        "gpu_scores": allocator.scores,
        "expected_memory_mb": state.add_expected_memory_mb,
        "gpu_memory_capacity_mb": state.gpu_memory_capacity_mb,
        "share_units": state.add_share_units if mode == MODE_SHARED else 1,
    }
    if nearest:
        slot = find_nearest_slot(
            ledger,
            config,
            count,
            search_start,
            duration,
            mode,
            _current_actor().uid,
            **search_arguments,
        )
    else:
        slot = find_earliest_slot(
            ledger,
            config,
            count,
            search_start,
            duration,
            mode,
            _current_actor().uid,
            allow_queue=True,
            **search_arguments,
        )
    if slot is None:
        scope = "selected GPUs" if fixed_gpus else f"{count} GPU"
        state.message = f"no {mode} slot for {scope} in the next {config.queue_search_hours}h"
        state.error = True
        return

    scheduled_start, gpus = slot
    state.add_selected_gpus = set(gpus)
    state.add_cursor_gpu = min(gpus)
    _frame_editor_window(
        state,
        scheduled_start,
        scheduled_start + duration,
        now,
        auto_zoom=True,
    )
    local_start = scheduled_start.astimezone()
    gpu_text = ",".join(map(str, gpus))
    search_kind = "nearest" if nearest else ("fixed earliest" if fixed_gpus else "earliest")
    state.message = (
        f"{search_kind} found {count} GPU [{gpu_text}] at "
        f"{_weekday_label(local_start)} {local_start:%m-%d %H:%M}; "
        f"{state.slot_minutes}m/col; {allocator.source}; Enter confirms"
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
    state.add_duration_steps = _default_editor_duration_steps(config.slot_minutes)
    _start_add_select(config, state)
    state.add_cursor_gpu = _nearest_enabled_gpu(config, cursor_gpu)
    state.add_selected_gpus = (
        {state.add_cursor_gpu} if state.add_cursor_gpu in config.enabled_gpus else set()
    )
    state.message = "add reset to 1 GPU / 30m / shared"


def _move_editor_start(state: TuiState, delta_steps: int) -> None:
    requested = state.add_start_steps + delta_steps
    if requested >= 0:
        state.add_start_steps = requested
    elif state.edit_mode and state.editor_view_start is not None:
        state.editor_view_start += timedelta(
            minutes=requested * state.booking_slot_minutes
        )
        state.add_start_steps = 0
    else:
        state.add_start_steps = 0
    state.add_search_anchor = _editor_selection_start(state)


def _editor_selection_start(state: TuiState) -> datetime:
    return _editor_view_start(state) + timedelta(
        minutes=state.add_start_steps * state.booking_slot_minutes
    )


def _quick_duration_steps(state: TuiState) -> int:
    quick_minutes = max(EDITOR_MIN_QUICK_MINUTES, state.slot_minutes)
    return max(
        1,
        (quick_minutes + state.booking_slot_minutes - 1)
        // state.booking_slot_minutes,
    )


def _quick_duration_text(state: TuiState) -> str:
    return _duration_text(
        timedelta(
            minutes=_quick_duration_steps(state) * state.booking_slot_minutes
        )
    )


def _editor_duration_text(state: TuiState) -> str:
    return _duration_text(
        timedelta(
            minutes=max(1, state.add_duration_steps) * state.booking_slot_minutes
        )
    )


def _default_editor_duration_steps(slot_minutes: int) -> int:
    return max(1, (30 + slot_minutes - 1) // slot_minutes)


def _configure_booking_slot(state: TuiState, slot_minutes: int) -> None:
    current_zoom = state.slot_minutes
    state.booking_slot_minutes = slot_minutes
    state.zoom_levels = tuple(sorted(set(ZOOM_LEVELS) | {slot_minutes}))
    state.zoom_index = min(
        range(len(state.zoom_levels)),
        key=lambda index: abs(state.zoom_levels[index] - current_zoom),
    )


def _frame_editor_window(
    state: TuiState,
    selection_start: datetime,
    selection_end: datetime,
    now: datetime,
    *,
    auto_zoom: bool,
) -> None:
    columns = max(24, state.timeline_columns)
    context_columns = min(EDITOR_CONTEXT_COLUMNS, max(1, columns // 8))
    now_anchor = _floor_to_add_step(now, state.booking_slot_minutes)

    if auto_zoom:
        chosen = len(state.zoom_levels) - 1
        for index in range(state.zoom_index, len(state.zoom_levels)):
            slot_minutes = state.zoom_levels[index]
            candidate_start = now_anchor - timedelta(minutes=context_columns * slot_minutes)
            candidate_end = candidate_start + timedelta(minutes=columns * slot_minutes)
            margin = timedelta(minutes=context_columns * slot_minutes)
            if candidate_start <= selection_start and selection_end + margin <= candidate_end:
                chosen = index
                break
        state.zoom_index = chosen

    slot_minutes = state.slot_minutes
    live_start = _floor_to_add_step(
        now_anchor - timedelta(minutes=context_columns * slot_minutes),
        state.booking_slot_minutes,
    )
    live_end = live_start + timedelta(minutes=columns * slot_minutes)
    margin = timedelta(minutes=context_columns * slot_minutes)
    if live_start <= selection_start and selection_end + margin <= live_end:
        view_start = live_start
    else:
        view_start = _floor_to_add_step(
            selection_start - margin,
            state.booking_slot_minutes,
        )

    state.editor_view_start = view_start
    state.add_start_steps = max(
        0,
        int(
            (selection_start - view_start).total_seconds()
            // (state.booking_slot_minutes * 60)
        ),
    )


def _availability_ledger(
    ledger: dict,
    state: TuiState,
    *,
    at: Optional[datetime] = None,
) -> dict:
    active = list_active(ledger, at or utc_now())
    if not state.edit_mode or not state.edit_reservation_id:
        return {**ledger, "reservations": active}
    return {
        **ledger,
        "reservations": [
            item
            for item in active
            if item.get("id") != state.edit_reservation_id
        ],
    }


def _clear_editor_feedback(state: TuiState) -> None:
    state.message = ""
    state.error = False


def _close_editor(state: TuiState) -> None:
    state.add_mode = False
    state.edit_mode = False
    state.edit_started = False
    state.edit_reservation_id = None
    state.editor_view_start = None
    state.add_search_anchor = None


def _editor_view_start(state: TuiState) -> datetime:
    if state.editor_view_start is not None:
        return state.editor_view_start
    return _default_timeline_view_start(
        utc_now(), state.slot_minutes, state.booking_slot_minutes
    )


def _own_reservation_index(store: LedgerStore, reservation_id: str) -> int:
    for index, reservation in enumerate(_active_reservations(store)):
        if reservation.get("id") == reservation_id:
            return index
    return -1


def _delete_selected(
    stdscr,
    config: Config,
    store: LedgerStore,
    state: TuiState,
) -> None:
    reservations = _active_reservations(store)
    if not reservations:
        state.message = "no reservation to delete"
        state.error = True
        return
    if state.selected < 0:
        state.message = "select a reservation before deleting"
        state.error = True
        return
    state.selected = min(state.selected, len(reservations) - 1)
    reservation = reservations[state.selected]
    if int(reservation.get("uid", -1)) != _current_actor().uid:
        state.message = "you can inspect this reservation, but only its owner can delete it"
        state.error = True
        return
    id_width = _visible_id_width(reservations)
    short_id = reservation["id"][:id_width]
    answer = _prompt_line(
        stdscr,
        f"Delete {short_id}? type yes",
        "",
        title="Cancel booking",
    )
    if answer != "yes":
        state.message = "delete cancelled"
        state.error = False
        return
    submit_cancellation(config, store, _current_actor(), reservation["id"])
    if store.last_warning:
        state.message = f"deleted {short_id}; warning: {store.last_warning}"
        state.error = True
    else:
        state.message = f"deleted {short_id}"
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
        _win_addstr(
            win,
            0,
            2,
            f" {title[: max(0, win_width - 6)]} ",
            COLOR_HEADER,
            curses.A_BOLD,
        )
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


def _prompt_line(stdscr, prompt: str, default: str = "", *, title: str = "Input") -> str:
    values = _prompt_fields(stdscr, title, [(prompt, default)])
    if values is None:
        return ""
    return values[0]


def _help_dialog(
    stdscr,
    initial_page: int = 0,
    *,
    tutorial: bool = False,
) -> None:
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
        dialog_name = "Tutorial" if tutorial else "Help"
        heading = f" GPUBK {dialog_name} {page + 1}/{len(HELP_PAGES)}  {title} "
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
        footer = f" Left/Right page   1-{len(HELP_PAGES)} jump   q/Esc/? close "
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


def _show_usage_summary(stdscr, config: Config) -> None:
    now = utc_now()
    actor = _current_actor()
    audit_store = UsageAuditStore(
        config.data_dir,
        config.lock_timeout_seconds,
        config.file_mode,
        config.dir_mode,
        config.storage_gid,
    )
    api = UsageQueryService(config, audit_store)
    users = api.users(
        start=now - timedelta(hours=24),
        end=now,
        resolution="auto",
        uid=actor.uid,
        limit=1,
    )
    samples = api.samples(
        start=now - timedelta(days=28),
        end=now,
        resolution="1d",
        uid=actor.uid,
        limit=1000,
    )
    lines = _usage_summary_lines(users, samples, now, width=72)
    _scroll_dialog(stdscr, "GPUBK my GPU use", lines)


def _usage_summary_lines(
    users_payload: dict,
    samples_payload: dict,
    end: datetime,
    *,
    width: int,
) -> List[str]:
    collector = users_payload.get("collector", {})
    collector_state = str(collector.get("state", "unknown"))
    lines = [
        "LAST 24 HOURS",
        f"Monitor {collector_state} | sampled past only; future reservations excluded",
    ]
    coverage = users_payload.get("coverage", {})
    if isinstance(coverage, dict) and coverage.get("first_sample_at") and coverage.get("last_sample_at"):
        try:
            first = parse_iso(str(coverage["first_sample_at"])).astimezone()
            last = parse_iso(str(coverage["last_sample_at"])).astimezone()
            lines.append(f"Recorded {first:%m-%d %H:%M} -> {last:%m-%d %H:%M}; unsampled gaps stay unknown")
        except ValueError:
            pass
    users = users_payload.get("users", [])
    if users:
        item = users[0]
        active = max(0, int(item.get("active_gpu_seconds", 0)))
        reserved = max(0, int(item.get("reserved_gpu_seconds", 0)))
        idle = max(0, int(item.get("idle_reserved_gpu_seconds", 0)))
        violations = max(0, int(item.get("violation_gpu_seconds", 0)))
        ratio = 0.0 if reserved <= 0 else min(1.0, active / reserved)
        bar_width = max(12, min(36, width - 35))
        active_cells = round(bar_width * ratio)
        idle_cells = max(0, bar_width - active_cells)
        lines.extend(
            (
                f"Active {format_usage_duration(active):>7}   "
                f"Reserved {format_usage_duration(reserved):>7}   Use {ratio * 100:5.1f}%",
                f"  {'█' * active_cells}{'░' * idle_cells}  active / reserved-idle",
                f"Peak VRAM {format_usage_memory(int(item.get('max_gpu_memory_mb', 0))):>7}   "
                f"Avg SM {_usage_sm_text(item.get('avg_sm_percent')):>6}   "
                f"Violation {format_usage_duration(violations)}",
                f"Reserved idle {format_usage_duration(idle)}",
            )
        )
    else:
        rollup = max(1, int(collector.get("rollup_seconds", 60)))
        started_at = collector.get("started_at")
        uptime = rollup
        if started_at:
            try:
                uptime = max(0, int((end - parse_iso(str(started_at))).total_seconds()))
            except ValueError:
                pass
        if collector_state in {"running", "degraded"} and uptime < rollup:
            lines.extend(
                (
                    "No finalized usage yet.",
                    f"First summary is expected in about {format_usage_duration(rollup - uptime)}.",
                )
            )
        elif collector_state in {"running", "degraded"}:
            lines.extend(
                (
                    "No recorded GPU use for your UID in the last 24 hours.",
                    f"Active bookings and attributed processes appear after each {format_usage_duration(rollup)} summary.",
                )
            )
        else:
            lines.extend(
                (
                    "Usage history is unavailable because the monitor is not healthy.",
                    "Run bk doctor --require-monitor or contact the administrator.",
                )
            )

    daily_rows, weekly_rows = build_activity_trends(
        samples_payload.get("records", []),
        end,
    )
    lines.extend(("", "LAST 7 DAYS  active/reserved GPU-hours"))
    lines.extend(_usage_trend_lines(daily_rows, width=width, weekly=False))
    lines.extend(("", "LAST 4 WEEKS  active/reserved GPU-hours"))
    lines.extend(_usage_trend_lines(weekly_rows, width=width, weekly=True))
    lines.extend(("", "Legend: █ active   ░ reserved but idle", "CLI detail: bk u   JSON/API: bk u -j"))
    return lines


def _usage_trend_lines(rows, *, width: int, weekly: bool) -> List[str]:
    active_hours = [values[0] / 3600 for _, values in rows]
    reserved_hours = [values[1] / 3600 for _, values in rows]
    peak = max(reserved_hours, default=0.0)
    bar_width = max(10, min(28, width - 35))
    lines = []
    for (period, _values), active, reserved in zip(rows, active_hours, reserved_hours):
        active_cells, idle_cells = activity_bar_cells(active, reserved, peak, bar_width)
        label = period.strftime("week %m-%d" if weekly else "%a %m-%d")
        bar = "█" * active_cells + "░" * idle_cells
        lines.append(
            f"{label:<11} {bar:<{bar_width}} {active:>6.1f}/{reserved:<6.1f}"
        )
    return lines


def _usage_sm_text(value: object) -> str:
    return "-" if value is None else f"{float(value):.1f}%"


def _show_gpu_details(
    stdscr,
    config: Config,
    store: LedgerStore,
    gpu_index: int,
) -> None:
    now = utc_now()
    active = list_active(store.load(), now)
    snapshots = _normalized_snapshots(config)
    gpu = next(
        (item for item in snapshots if item.index == gpu_index),
        GpuSnapshot(index=gpu_index, name="unknown"),
    )
    usage = classify_process_usage(
        snapshots,
        active,
        now,
        container_groups=config.container_attribution_groups,
    ).get(gpu_index, [])
    related = [item for item in active if gpu_index in item.get("gpus", [])]
    id_width = _visible_id_width(active)
    util = f"{gpu.utilization_percent}%" if gpu.utilization_percent is not None else "n/a"
    memory = (
        f"{gpu.memory_used_mb / 1024:.1f}/{gpu.memory_total_mb / 1024:.1f} GiB"
        if gpu.memory_total_mb
        else "n/a"
    )
    lines = [f"Utilization {util}   VRAM {memory}   Source {gpu.source}", "", "RESERVATIONS"]
    lines.append(_gpu_booking_header(160, id_width))
    if related:
        lines.extend(
            _gpu_booking_line(
                item,
                160,
                config.max_shared_users,
                id_width,
                number=number,
            )
            for number, item in enumerate(related, start=1)
        )
    else:
        lines.append("No active reservations")
    lines.extend(("", "PROCESSES", _process_table_header(160, id_width)))
    if usage:
        lines.extend(_process_table_line(item, 160, id_width) for item in usage)
    else:
        lines.append("No GPU processes")
    _scroll_dialog(stdscr, f"GPUBK GPU {gpu_index}", lines)


def _show_selected_reservation_details(
    stdscr,
    config: Config,
    store: LedgerStore,
    state: TuiState,
) -> None:
    active = _active_reservations(store)
    if state.selected < 0 or not active:
        state.message = "Down selects a reservation; Enter opens its details"
        state.error = False
        return
    state.selected = min(state.selected, len(active) - 1)
    reservation = active[state.selected]
    lines = _reservation_detail_lines(reservation, config, _current_actor())
    if stdscr is None:
        state.message = f"reservation {str(reservation.get('id', ''))[:_visible_id_width(active)]} details"
    else:
        _scroll_dialog(stdscr, f"GPUBK reservation {str(reservation.get('id', ''))[:8]}", lines)
    state.error = False


def _reservation_detail_lines(reservation: dict, config: Config, actor: Actor) -> List[str]:
    public = public_reservation(reservation, actor, config.max_shared_users)
    start = parse_iso(str(public["start_at"]))
    end = parse_iso(str(public["end_at"]))
    owner = f"{public.get('username', 'unknown')} (UID {public.get('uid', 'unknown')})"
    access = "you can edit or cancel" if public.get("mine") else "read-only"
    gpus = ",".join(str(gpu) for gpu in public.get("gpus", [])) or "none"
    lines = [
        f"ID: {public.get('id', '')}",
        f"Owner: {owner} - {access}",
        f"Status: {public.get('status', 'unknown')}",
        f"Mode: {public.get('mode', 'unknown')}",
        f"GPUs: {gpus}",
    ]
    if public.get("mode") == MODE_SHARED:
        lines.append(
            "Shared slots/GPU: "
            f"request {public.get('share_units_per_gpu', 1)}; "
            f"server max {public.get('share_capacity_units_per_gpu', config.max_shared_users)}"
        )
        expected_memory = public.get("expected_memory_mb_per_gpu")
        memory_text = (
            f"{int(expected_memory) / 1024:.1f} GiB"
            if isinstance(expected_memory, int) and expected_memory > 0
            else "automatic estimate"
        )
        lines.append(f"Expected VRAM/GPU: {memory_text}")
    lines.extend(
        (
            f"Start: {start.astimezone():%a %Y-%m-%d %H:%M %z}",
            f"End: {end.astimezone():%a %Y-%m-%d %H:%M %z}",
            f"Duration: {_duration_detail_text(end - start)}",
        )
    )
    job = public.get("job")
    if isinstance(job, dict):
        lines.append(
            f"Scheduled job: {job.get('status', 'unknown')} - {job.get('summary', 'private command')}"
        )
    return lines


def _scroll_dialog(stdscr, title: str, lines: Sequence[str]) -> None:
    height, width = stdscr.getmaxyx()
    win_height = max(8, height - 4)
    win_width = max(50, width - 4)
    top = max(1, (height - win_height) // 2)
    left = max(1, (width - win_width) // 2)
    win = curses.newwin(win_height, win_width, top, left)
    win.keypad(True)
    page_rows = max(1, win_height - 4)
    offset = 0
    while True:
        win.erase()
        win.box()
        _win_addstr(win, 0, 2, f" {title} ", COLOR_HEADER, curses.A_BOLD)
        for row, line in enumerate(lines[offset : offset + page_rows], 1):
            section = line in {
                "RESERVATIONS",
                "PROCESSES",
                "LAST 24 HOURS",
                "LAST 7 DAYS  active/reserved GPU-hours",
                "LAST 4 WEEKS  active/reserved GPU-hours",
            }
            color = COLOR_SELECTED if section else COLOR_MINE if "█" in line else 0
            attr = curses.A_BOLD if color else 0
            _win_addstr(win, row, 2, line[: max(0, win_width - 4)], color, attr)
        footer = " Up/Down scroll  PgUp/PgDn page  Home/End  q/Esc close "
        _win_addstr(win, win_height - 2, 2, footer[: win_width - 4], COLOR_HEADER)
        win.refresh()
        key = win.getch()
        maximum = max(0, len(lines) - page_rows)
        if key in (27, ord("q"), ord("Q"), curses.KEY_ENTER, 10, 13):
            return
        if key in (curses.KEY_DOWN, ord("j")):
            offset = min(maximum, offset + 1)
        elif key in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)
        elif key == curses.KEY_NPAGE:
            offset = min(maximum, offset + page_rows)
        elif key == curses.KEY_PPAGE:
            offset = max(0, offset - page_rows)
        elif key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = maximum


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
    slot_minutes = config.slot_minutes
    start = view_start + timedelta(minutes=state.add_start_steps * slot_minutes)
    end = start + timedelta(minutes=max(1, state.add_duration_steps) * slot_minutes)
    mode = state.add_booking_mode if state.add_booking_mode in {MODE_SHARED, MODE_EXCLUSIVE} else MODE_SHARED
    now = utc_now()
    earliest = (
        _ceil_to_add_step(now, slot_minutes)
        if state.edit_mode
        else _floor_to_add_step(now, slot_minutes)
    )
    if start < earliest and not state.edit_started:
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
            share_units=state.add_share_units,
            share_capacity=config.max_shared_users,
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
            share_units=state.add_share_units,
            share_capacity=config.max_shared_users,
        )
    disabled = sorted(set(selected) & set(config.disabled_gpus))
    if disabled:
        return AddPreview(
            start,
            end,
            selected,
            cursor_gpu,
            mode,
            False,
            f"GPU {','.join(map(str, disabled))} disabled by the administrator",
            blink=state.add_mode,
            share_units=state.add_share_units,
            share_capacity=config.max_shared_users,
        )
    if state.edit_started:
        reservation = next(
            (
                item
                for item in ledger.get("reservations", [])
                if item.get("id") == state.edit_reservation_id
            ),
            None,
        )
        if reservation is None:
            return AddPreview(
                start, end, selected, cursor_gpu, mode, False,
                "reservation not found", blink=False,
                share_units=state.add_share_units,
                share_capacity=config.max_shared_users,
            )
        earliest_end = ceil_to_slot(now, slot_minutes)
        if end < earliest_end or end <= now:
            return AddPreview(
                start, end, selected, cursor_gpu, mode, False,
                f"end must be at or after {earliest_end.astimezone():%m-%d %H:%M}",
                blink=False,
                share_units=state.add_share_units,
                share_capacity=config.max_shared_users,
            )
        current_end = parse_iso(reservation["end_at"])
        if end > current_end:
            extension_start = max(current_end, earliest_end)
            policy_reason = booking_policy_conflict(
                config, extension_start, end, now=now
            )
            if policy_reason:
                return AddPreview(
                    start, end, selected, cursor_gpu, mode, False, policy_reason,
                    blink=False,
                    share_units=state.add_share_units,
                    share_capacity=config.max_shared_users,
                )
            availability_ledger = _availability_ledger(ledger, state, at=now)
            for gpu in selected:
                ok, reason = availability_detail(
                    availability_ledger,
                    gpu,
                    extension_start,
                    end,
                    mode,
                    _current_actor().uid,
                    config.max_shared_users,
                    state.add_expected_memory_mb,
                    state.gpu_memory_capacity_mb,
                    config.shared_memory_reserve_mb,
                    state.add_share_units if mode == MODE_SHARED else 1,
                )
                if not ok:
                    return AddPreview(
                        start, end, selected, cursor_gpu, mode, False, reason,
                        blink=False,
                        share_units=state.add_share_units,
                        share_capacity=config.max_shared_users,
                    )
        return AddPreview(
            start, end, selected, cursor_gpu, mode, True,
            blink=False,
            share_units=state.add_share_units,
            share_capacity=config.max_shared_users,
        )
    availability_ledger = _availability_ledger(ledger, state, at=now)
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
            state.add_share_units if mode == MODE_SHARED else 1,
        )
        if not ok:
            return AddPreview(
                start,
                end,
                selected,
                cursor_gpu,
                mode,
                False,
                reason,
                blink=state.add_mode,
                share_units=state.add_share_units,
                share_capacity=config.max_shared_users,
            )
    return AddPreview(
        start,
        end,
        selected,
        cursor_gpu,
        mode,
        True,
        blink=state.add_mode,
        share_units=state.add_share_units,
        share_capacity=config.max_shared_users,
    )


def _preview_status_text(preview: AddPreview, operation: str = "add") -> str:
    local_start = preview.start.astimezone()
    local_end = preview.end.astimezone()
    duration = _duration_detail_text(local_end - local_start)
    gpu_text = ",".join(map(str, preview.selected_gpus)) or "-"
    status = "ok" if preview.valid else preview.reason
    share = (
        f" share={share_text(preview.share_units, preview.share_capacity)}"
        if preview.mode == MODE_SHARED
        else ""
    )
    return (
        f"{operation} {preview.mode}{share} GPU={gpu_text} "
        f"{_weekday_label(local_start)} {local_start:%m-%d %H:%M}->{local_end:%m-%d %H:%M} "
        f"{duration} | {status}"
    )


def _editor_shared_slot_usage(
    config: Config,
    store: LedgerStore,
    state: TuiState,
) -> dict[int, int]:
    view_start = _editor_view_start(state)
    start = view_start + timedelta(
        minutes=state.add_start_steps * state.booking_slot_minutes
    )
    end = start + timedelta(
        minutes=state.add_duration_steps * state.booking_slot_minutes
    )
    active = [
        item
        for item in list_active(store.load())
        if str(item.get("id")) != state.edit_reservation_id
    ]
    selected = state.add_selected_gpus or {state.add_cursor_gpu}
    usage = {}
    for gpu in sorted(selected):
        overlapping = [
            item
            for item in active
            if gpu in item.get("gpus", [])
            and parse_iso(item["start_at"]) < end
            and start < parse_iso(item["end_at"])
        ]
        if any(item.get("mode") == MODE_EXCLUSIVE for item in overlapping):
            usage[gpu] = config.max_shared_users
        else:
            usage[gpu] = shared_capacity_units_for_gpu(
                overlapping,
                gpu,
                start,
                end,
                config.max_shared_users,
            )
    return usage


def _editor_slot_usage_text(usage: dict[int, int]) -> str:
    if not usage:
        return "used=0"
    if len(usage) <= 4:
        return "used=" + ",".join(
            f"G{gpu}:{used}" for gpu, used in sorted(usage.items())
        )
    values = list(usage.values())
    return f"used={min(values)}-{max(values)} on {len(values)} GPUs"


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


def _table_header(
    width: int,
    gpu_count: int = GPU_MAP_SIZE,
    id_width: int = MIN_SHORT_ID_WIDTH,
) -> str:
    gpu_width = _reservation_gpu_width(width, gpu_count)
    gpu_header = "GPU"
    if width < 100:
        return f"{'#':>3} {'ID':<{id_width}} {'User':<10} {'M':<1} {gpu_header:<{gpu_width}} {'Req':<5} {'Start':<11} {'End':<11} {'Dur':<7}"
    return f"{'#':>3} {'ID':<{id_width}} {'User':<16} {'Mode':<4} {gpu_header:<{gpu_width}} {'Req':<5} {'Start':<11} {'End':<11} {'Dur':<7}"


def _reservation_table_line(
    reservation: dict,
    prefix: str,
    width: int,
    active: Sequence[dict],
    shared_limit: int,
    gpu_count: int = GPU_MAP_SIZE,
    id_width: int = MIN_SHORT_ID_WIDTH,
) -> str:
    start = parse_iso(reservation["start_at"]).astimezone()
    end = parse_iso(reservation["end_at"]).astimezone()
    duration = _truncate(_duration_text(end - start), 7)
    user_width = 10 if width < 100 else 16
    gpu_width = _reservation_gpu_width(width, gpu_count)
    mode_label = _mode_mark(reservation) if width < 100 else reservation.get("mode", "")[:4]
    cap = _capacity_text(reservation, active, shared_limit)
    return (
        f"{prefix:>3} {reservation['id'][:id_width]:<{id_width}} "
        f"{_truncate(str(reservation.get('username', '')), user_width):<{user_width}} "
        f"{mode_label:<{1 if width < 100 else 4}} "
        f"{_reservation_gpu_text(reservation.get('gpus', []), gpu_count, gpu_width):<{gpu_width}} "
        f"{cap:<5} "
        f"{start:%m-%d %H:%M} {end:%m-%d %H:%M} {duration:<7}"
    )


def _reservation_gpu_text(gpus: Sequence[int], gpu_count: int, width: int) -> str:
    if gpu_count <= GPU_POSITION_MAP_MAX:
        selected = {int(gpu) for gpu in gpus if isinstance(gpu, int)}
        return "".join(str(gpu) if gpu in selected else FREE_CHAR for gpu in range(gpu_count))
    return _compact_gpus(gpus, width)


def _reservation_gpu_width(width: int, gpu_count: int) -> int:
    if gpu_count <= GPU_POSITION_MAP_MAX:
        return max(3, gpu_count)
    return 8 if width < 100 else 12


def _capacity_text(reservation: dict, active: Sequence[dict], shared_limit: int) -> str:
    if reservation.get("mode") == MODE_EXCLUSIVE:
        return "-"
    return f"{reservation_share_units(reservation, shared_limit)}/{shared_limit}"


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


def _share_lane_label(
    gpu: int,
    reservation: dict,
    shared_limit: int,
    hidden: int = 0,
    id_width: int = MIN_SHORT_ID_WIDTH,
) -> str:
    username = _truncate(str(reservation.get("username", "?")), 10)
    suffix = f" +{hidden}" if hidden else ""
    units = reservation_share_units(reservation, shared_limit)
    return (
        f"G{gpu} {units}/{shared_limit} "
        f"{str(reservation.get('id', ''))[:id_width]} {username}{suffix}"
    )


def _peak_shared_count_for_gpu(
    active: Sequence[dict],
    gpu: int,
    start: datetime,
    end: datetime,
    reservation_index: Optional[ReservationIndex] = None,
    shared_limit: int = 1,
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
                sum(
                    reservation_share_units(item.record, shared_limit)
                    for item in spans
                    if item.start < right and left < item.end
                )
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
            sum(
                reservation_share_units(item, shared_limit)
                for item in shared
                if parse_iso(item["start_at"]) < right
                and left < parse_iso(item["end_at"])
            ),
        )
    return peak


def _gpu_label(
    gpu: GpuSnapshot,
    width: int,
    peak_shared: int = 0,
    shared_limit: int = 1,
    violations: int = 0,
    exclusive: bool = False,
    disabled: bool = False,
) -> str:
    gpu_field = _truncate(str(gpu.index), 2)
    capacity_field = _truncate(
        "OFF"
        if disabled
        else (f"X/{shared_limit}" if exclusive else f"{peak_shared}/{shared_limit}"),
        5,
    )
    extras = [f"!{violations}"] if violations else []
    util = _truncate(
        f"{gpu.utilization_percent}%" if gpu.utilization_percent is not None else "-",
        4,
    )
    memory = "-"
    if gpu.memory_total_mb:
        free_gib = max(0, gpu.memory_total_mb - gpu.memory_used_mb) / 1024
        memory = f"{free_gib:.0f}G" if free_gib >= 99.95 else f"{free_gib:.1f}G"
    core = f"{gpu_field:>2} {capacity_field:>5} {util:>4}|{memory:>5}"
    if gpu.temperature_c is not None:
        extras.append(f"{gpu.temperature_c}C")
    if gpu.processes:
        extras.append(f"P{len(gpu.processes)}")
    return _append_complete_metrics(core, extras, width)


def _gpu_row_label(
    gpu: GpuSnapshot,
    width: int,
    peak_shared: int = 0,
    shared_limit: int = 1,
    violations: int = 0,
    focused: bool = False,
    exclusive: bool = False,
    disabled: bool = False,
) -> str:
    marker = ">" if focused else " "
    content = _gpu_label(
        gpu,
        max(0, width - 1),
        peak_shared,
        shared_limit,
        violations,
        exclusive,
        disabled,
    )
    return (marker + content)[:width].ljust(width)


def _gpu_is_exclusive_now(
    active: Sequence[dict],
    gpu: int,
    now: datetime,
    reservation_index: Optional[ReservationIndex] = None,
) -> bool:
    if reservation_index is not None:
        return any(
            span.mode == MODE_EXCLUSIVE and span.record.get("status") == STATUS_ACTIVE
            for span in reservation_index.overlapping(gpu, now, now + timedelta(seconds=1))
        )
    return any(
        item.get("status") == STATUS_ACTIVE
        and item.get("mode") == MODE_EXCLUSIVE
        and gpu in item.get("gpus", [])
        and parse_iso(item["start_at"]) <= now < parse_iso(item["end_at"])
        for item in active
    )


def _timeline_label_width(terminal_width: int) -> int:
    return min(24, max(21, terminal_width // 6))


def _gpu_metrics_header(width: int) -> str:
    return f"{'GPU':>3} {'Cap':>5} {'Util':>4}|{'Free':>5}"[:width].ljust(width)


def _append_complete_metrics(core: str, extras: Sequence[str], width: int) -> str:
    text = core.rstrip()
    for metric in extras:
        candidate = f"{text} {metric}"
        if len(candidate) > width:
            break
        text = candidate
    return text[:width].ljust(width)


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


def _shared_visual_slots(
    shared_items: Sequence[dict], capacity_units: int
) -> List[Optional[dict]]:
    remaining = [
        [item, reservation_share_units(item, max(1, capacity_units))]
        for item in shared_items
    ]
    slots: List[Optional[dict]] = []
    while any(units > 0 for _item, units in remaining):
        for entry in remaining:
            if entry[1] <= 0:
                continue
            slots.append(entry[0])
            entry[1] -= 1
    slots.extend([None] * max(0, capacity_units - len(slots)))
    return slots or [None]


def _shared_visual_pair(
    slots: Sequence[Optional[dict]], col: int
) -> Tuple[Optional[dict], Optional[dict]]:
    count = len(slots)
    top_index = (max(0, col) * 2) % count
    return slots[top_index], slots[(top_index + 1) % count]


def _render_shared_pair(
    top_item: Optional[dict],
    bottom_item: Optional[dict],
    color_map: dict[str, int],
    selected_id: Optional[str],
    col: int,
) -> Tuple[str, int, int]:
    if top_item is None and bottom_item is None:
        return FREE_CHAR, COLOR_FREE, 0
    visible = top_item or bottom_item
    if visible is None:
        return FREE_CHAR, COLOR_FREE, 0
    selected = selected_id and selected_id in {
        str(top_item.get("id")) if top_item is not None else "",
        str(bottom_item.get("id")) if bottom_item is not None else "",
    }
    attr = curses.A_BOLD | (curses.A_BLINK if selected else 0)
    color = _reservation_color(visible, color_map)
    if top_item is None:
        return LOWER_SPLIT_CHAR, color, attr
    if bottom_item is None:
        return SPLIT_CHAR, color, attr
    if top_item.get("id") == bottom_item.get("id"):
        return BAR_CHAR, color, attr
    mixed_color = _mixed_color_pair(
        _reservation_color(top_item, color_map),
        _reservation_color(bottom_item, color_map),
    )
    if mixed_color is None:
        return WEAVE_CHARS[col % len(WEAVE_CHARS)], color, attr
    return SPLIT_CHAR, mixed_color, attr


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


def _clear_now_label_slot(line: str, now_col: int) -> Tuple[str, int]:
    label_col = min(max(0, now_col - 1), max(0, len(line) - 3))
    chars = list(line)
    clear_start = max(0, label_col - 1)
    clear_end = min(len(chars), label_col + 4)
    chars[clear_start:clear_end] = [" "] * (clear_end - clear_start)
    return "".join(chars), label_col


def _default_timeline_view_start(
    now: datetime,
    slot_minutes: int,
    booking_slot_minutes: int = DEFAULT_SLOT_MINUTES,
) -> datetime:
    anchor = _floor_to_add_step(now, booking_slot_minutes)
    return _floor_to_add_step(
        anchor - timedelta(minutes=NOW_CONTEXT_COLUMNS * max(1, slot_minutes)),
        booking_slot_minutes,
    )


def _timeline_view_start(now: datetime, state: TuiState) -> datetime:
    return _default_timeline_view_start(
        now, state.slot_minutes, state.booking_slot_minutes
    ) + timedelta(
        minutes=state.offset_slots * state.slot_minutes
    )


def _axis_tick_minutes(slot_minutes: int) -> Tuple[int, int]:
    if slot_minutes <= 5:
        return 15, 30
    if slot_minutes <= 15:
        return 30, 60
    if slot_minutes <= 30:
        return 60, 120
    if slot_minutes <= 60:
        return 180, 360
    if slot_minutes <= 120:
        return 360, 720
    if slot_minutes <= 240:
        return 720, 1440
    if slot_minutes <= 720:
        return 1440, 2880
    return 1440, 10080


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


def _duration_detail_text(delta: timedelta) -> str:
    compact = _duration_text(delta)
    minutes = max(0, int(delta.total_seconds() // 60))
    days, remainder = divmod(minutes, 24 * 60)
    if not days:
        return compact
    hours, mins = divmod(remainder, 60)
    parts = [f"{days}d"]
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    return f"{compact} ({''.join(parts)})"


def _visible_id_width(reservations: Sequence[dict]) -> int:
    ids = sorted(
        {
            str(item.get("id", ""))
            for item in reservations
            if str(item.get("id", ""))
        }
    )
    for width in range(MIN_SHORT_ID_WIDTH, MAX_SHORT_ID_WIDTH + 1):
        if len({value[:width] for value in ids}) == len(ids):
            return width
    return MAX_SHORT_ID_WIDTH


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


def _ceil_to_add_step(
    value: datetime,
    slot_minutes: int = DEFAULT_SLOT_MINUTES,
) -> datetime:
    return ceil_to_slot(value, slot_minutes)


def _floor_to_add_step(
    value: datetime,
    slot_minutes: int = DEFAULT_SLOT_MINUTES,
) -> datetime:
    return floor_to_slot(value, slot_minutes)


def _own_reservations(store: LedgerStore) -> List[dict]:
    actor = _current_actor()
    return [item for item in list_active(store.load()) if int(item.get("uid")) == actor.uid]


def _active_reservations(store: LedgerStore) -> List[dict]:
    return list_active(store.load())


def _selected_reservation_id(active: Sequence[dict], state: TuiState) -> Optional[str]:
    if not active or state.selected < 0:
        return None
    if state.selected >= len(active):
        state.selected = len(active) - 1
    return str(active[state.selected].get("id"))


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
    print("GPUBK TUI fallback")
    print(f"{administrator_display_lines(administrator_info(config))[0]}; details: bk info")
    print(
        f"data={config.data_dir} shared_capacity={config.max_shared_users} slots/GPU "
        f"enabled_GPUs={len(config.enabled_gpus)}/{config.gpu_count}"
    )
    if config.disabled_gpus:
        print(f"administrator-disabled GPUs: {','.join(map(str, config.disabled_gpus))}")
    print("active reservations:")
    active = list_active(store.load(), now)
    id_width = _visible_id_width(active)
    for reservation in active:
        gpus = ",".join(str(item) for item in reservation.get("gpus", []))
        share = (
            f" share={share_text(reservation_share_units(reservation, config.max_shared_users), config.max_shared_users)}"
            if reservation.get("mode") == MODE_SHARED
            else ""
        )
        print(
            f"- {reservation['id'][:id_width]} {reservation['mode']}{share} "
            f"GPU={gpus} {format_local_range(reservation['start_at'], reservation['end_at'])}"
        )
