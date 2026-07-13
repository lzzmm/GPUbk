import os
import curses
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bk.admin_info import AdministratorInfo
from bk.config import Config
from bk.gpu import GpuProcessSnapshot, GpuSnapshot
from bk.models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingRequest
from bk.schedule_index import ReservationIndex
from bk.scheduler import add_booking
from bk.service import submit_cancellation
from bk.storage import LedgerStore
from bk.tui import (
    AddPreview,
    BAR_CHAR,
    COLOR_PREVIEW_EXCLUSIVE,
    COLOR_PREVIEW_SHARED,
    FOCUS_GPUS,
    FOCUS_RESERVATIONS,
    HELP_PAGES,
    MIXED_COLOR_PAIRS,
    NOW_CHAR,
    SPLIT_CHAR,
    TuiState,
    WEAVE_CHARS,
    _build_add_preview,
    _capacity_text,
    _cell_for_gpu,
    _clear_now_label_slot,
    _collector_label,
    _date_label,
    _decorate_timeline_cell,
    _default_timeline_view_start,
    _duration_detail_text,
    _duration_text,
    _editor_banner_text,
    _editor_shared_slot_usage,
    _editor_slot_usage_text,
    _footer_label,
    _gpu_label,
    _gpu_metrics_header,
    _gpu_row_label,
    _gpu_view_anchor,
    _gpu_view_start,
    _header_lines,
    _handle_add_key,
    _handle_key,
    _hour_label,
    _init_curses,
    _load_edit_state,
    _minute_label,
    _move_focus_down,
    _move_focus_up,
    _own_reservation_index,
    _pan_timeline,
    _preview_cell_for_gpu,
    _process_table_line,
    _reservation_palette,
    _reservation_color_map,
    _reservation_gpu_text,
    _reservation_detail_lines,
    _reservation_view_start,
    _refresh_collector_status,
    _refresh_worker_status,
    _resolve_tui_theme,
    _selected_share_detail,
    _shared_weave_pair,
    _start_add_select,
    _start_edit_select,
    _table_header,
    _timeline_label_width,
    _timeline_now_col,
    _timeline_selected_id,
    _time_axis_lines,
    _theme_color_pairs,
    _toggle_focus,
    _visible_shared_reservations,
    _visible_id_width,
    _weekday_label,
    _worker_label,
)
from bk.timeparse import parse_iso, utc_now
from bk.usage import ProcessUsage, USAGE_AUTHORIZED, USAGE_UNRESERVED


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ceil_5m(value):
    timestamp = int(value.timestamp())
    remainder = timestamp % 300
    if remainder:
        timestamp += 300 - remainder
    return datetime.fromtimestamp(timestamp, timezone.utc)


def reservation(rid, uid, mode, gpus, start, end):
    return {
        "id": rid,
        "op_id": f"{rid}-op",
        "uid": uid,
        "username": f"user{uid}",
        "gpus": gpus,
        "mode": mode,
        "start_at": iso(start),
        "end_at": iso(end),
        "status": "active",
        "created_at": iso(start),
        "updated_at": iso(start),
    }


class TuiAddPreviewTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(data_dir=Path("/tmp/bk-tui-test"), gpu_count=2, max_shared_users=2)
        self.start = ceil_5m(utc_now() + timedelta(days=1))
        self.end = self.start + timedelta(minutes=30)

    def state(self, *, mode=MODE_SHARED, gpus=None, duration_steps=6):
        return TuiState(
            add_mode=True,
            add_cursor_gpu=0,
            add_start_steps=0,
            add_duration_steps=duration_steps,
            add_selected_gpus=set([0] if gpus is None else gpus),
            add_booking_mode=mode,
        )

    def ledger(self, reservations):
        return {"version": 1, "reservations": reservations}

    def test_compact_header_and_footers_fit_minimum_terminal_width(self):
        config = Config(
            data_dir=Path("/home/example/.local/share/a-very-long-bk-trial-name"),
            gpu_count=8,
            max_shared_users=3,
            tui_refresh_seconds=2.5,
        )
        width = 72
        title, details = _header_lines(config, self.start, self.start, self.end, width, TuiState())

        self.assertLessEqual(len(title), width - 1)
        self.assertLessEqual(len(details), width - 1)
        self.assertTrue(title.endswith("5m"), title)
        self.assertTrue(details.endswith("capacity"), details)
        self.assertIn("monitor=not-seen", details)
        self.assertIn("worker=idle", details)
        self.assertNotIn("data=", details)

        preview = AddPreview(self.start, self.end, (0,), 0, MODE_SHARED, True, blink=True)
        variants = [
            (_footer_label(TuiState(), None, width), "n NOW", "q quit"),
            (_footer_label(TuiState(focus=FOCUS_GPUS), None, width), "n NOW", "q quit"),
            (_footer_label(TuiState(add_mode=True), preview, width), "f first", "?"),
            (_footer_label(TuiState(edit_mode=True), preview, width), "f first", "?"),
        ]
        for footer, control, ending in variants:
            self.assertLessEqual(len(footer), width - 1)
            self.assertIn(control, footer)
            self.assertTrue(footer.rstrip().endswith(ending), footer)

    def test_editor_footer_keeps_commit_cancel_and_primary_controls_at_eighty_columns(self):
        preview = AddPreview(self.start, self.end, (0,), 0, MODE_SHARED, True, blink=True)

        add_footer = _footer_label(TuiState(add_mode=True), preview, 80)
        edit_footer = _footer_label(TuiState(edit_mode=True), preview, 80)

        for footer in (add_footer, edit_footer):
            self.assertLessEqual(len(footer), 79)
            self.assertIn("Space GPU", footer)
            self.assertIn("s/x", footer)
            self.assertIn("f first", footer)
            self.assertIn("Esc", footer)
            self.assertTrue(footer.endswith("?"), footer)
        self.assertIn("Enter book", add_footer)
        self.assertIn("Enter save", edit_footer)

    def test_reservation_view_follows_selection_without_renumbering(self):
        self.assertEqual(_reservation_view_start(12, 4, -1), 0)
        self.assertEqual(_reservation_view_start(12, 4, 3), 0)
        self.assertEqual(_reservation_view_start(12, 4, 4), 1)
        self.assertEqual(_reservation_view_start(12, 4, 9), 6)
        self.assertEqual(_reservation_view_start(12, 4, 11), 8)
        self.assertEqual(_reservation_view_start(3, 4, 2), 0)

    def test_gpu_view_follows_focus_in_a_short_terminal(self):
        self.assertEqual(_gpu_view_start(8, 3, 0), 0)
        self.assertEqual(_gpu_view_start(8, 3, 2), 0)
        self.assertEqual(_gpu_view_start(8, 3, 3), 1)
        self.assertEqual(_gpu_view_start(8, 3, 6), 4)
        self.assertEqual(_gpu_view_start(8, 3, 7), 5)
        self.assertEqual(_gpu_view_start(8, 8, 7), 0)

    def test_gpu_view_anchor_follows_editor_focus_or_selected_reservation(self):
        selected = reservation("selected", os.getuid(), MODE_SHARED, [6], self.start, self.end)

        self.assertEqual(
            _gpu_view_anchor(TuiState(add_mode=True, add_cursor_gpu=7), [selected], "selected"),
            7,
        )
        self.assertEqual(
            _gpu_view_anchor(TuiState(focus=FOCUS_GPUS, selected_gpu=5), [selected], "selected"),
            5,
        )
        self.assertEqual(_gpu_view_anchor(TuiState(), [selected], "selected"), 6)
        self.assertEqual(_gpu_view_anchor(TuiState(), [selected], None), 0)

    def test_reservation_details_are_readable_but_keep_other_users_read_only(self):
        reservation = {
            "id": "abcdef12-3456-7890-abcd-ef1234567890",
            "uid": 1001,
            "username": "alice",
            "mode": MODE_SHARED,
            "gpus": [0, 2],
            "start_at": "2030-01-01T09:00:00Z",
            "end_at": "2030-01-01T10:30:00Z",
            "status": "active",
            "share_units": 3,
            "expected_memory_mb": 12288,
        }

        lines = _reservation_detail_lines(
            reservation,
            Config(data_dir=Path("/tmp/gpubk-detail-test"), gpu_count=4, max_shared_users=4),
            Actor(uid=1002, username="bob"),
        )
        text = "\n".join(lines)

        self.assertIn(reservation["id"], text)
        self.assertIn("alice (UID 1001) - read-only", text)
        self.assertIn("GPUs: 0,2", text)
        self.assertIn("request 3; server max 4", text)
        self.assertIn("Expected VRAM/GPU: 12.0 GiB", text)
        self.assertIn("Duration: 1h30m", text)

    def test_help_pages_explain_ambiguous_keys_and_offer_a_quick_tour(self):
        pages = {title: dict(entries) for title, entries in HELP_PAGES}

        self.assertIn("Quick Tour", pages)
        self.assertIn("auto-refresh", pages["Navigate"]["r"])
        self.assertIn("live NOW", pages["Navigate"]["n"])
        self.assertIn("administrator", pages["Navigate"]["i"])
        self.assertIn("any GPUs", pages["Add / Edit"]["f"])
        self.assertIn("exactly the selected GPUs", pages["Add / Edit"]["g"])
        self.assertIn("restore", pages["Add / Edit"]["r"])
        self.assertIn("collector health", pages["Timeline"]["Monitor"])
        self.assertIn("scheduled-command worker", pages["Timeline"]["Worker"])
        self.assertIn("bk u", pages["Quick Tour"])
        self.assertIn("administrator", pages["Quick Tour"]["i"])

        minimum_window_width = 70
        for _title, entries in HELP_PAGES:
            key_width = min(
                max(14, max((len(key) for key, _description in entries), default=0) + 2),
                max(14, minimum_window_width // 3),
            )
            description_width = minimum_window_width - (3 + key_width) - 1
            for key, description in entries:
                if key:
                    self.assertLessEqual(len(key), key_width - 2)
                    self.assertLessEqual(len(description), description_width, (key, description))

    def test_info_key_opens_the_administrator_contact_dialog(self):
        config = Config(data_dir=Path("/tmp/gpubk-tui-info"))
        store = LedgerStore(config.data_dir)
        state = TuiState()
        info = AdministratorInfo(
            uid=1003,
            username="chenyuhan",
            full_name="Chen Yuhan",
            other="admin@example.com",
        )

        with (
            mock.patch("bk.tui.administrator_info", return_value=info),
            mock.patch("bk.tui._message_dialog") as dialog,
        ):
            _handle_key(mock.Mock(), ord("i"), config, store, state)

        dialog.assert_called_once()
        self.assertEqual(dialog.call_args.args[1], "GPUBK administrator")
        self.assertIn("Administrator: Chen Yuhan", dialog.call_args.args[2][0])
        self.assertIn("admin@example.com", dialog.call_args.args[2][-1])

    def test_collector_labels_are_compact_and_defensive(self):
        expected = {
            "running": "OK",
            "degraded": "DEG",
            "stale": "STALE",
            "stopped": "STOP",
            "clock-skew": "CLOCK",
            "topology-mismatch": "TOPO",
            "not-seen": "--",
        }
        for state, label in expected.items():
            self.assertEqual(_collector_label({"state": state}), label)
        self.assertEqual(_collector_label({"state": "future-state"}), "ERR")
        self.assertEqual(_collector_label(None), "ERR")

    def test_collector_status_refresh_is_side_effect_free_and_rate_limited(self):
        config = Config(data_dir=Path("/tmp/bk-tui-collector"), gpu_count=2)
        state = TuiState()
        first = self.start
        payload = {"state": "running", "fresh": True}

        with mock.patch(
            "bk.tui.UsageAuditStore.load_collector_status", return_value=payload
        ) as load:
            _refresh_collector_status(config, state, first)
            _refresh_collector_status(config, state, first + timedelta(seconds=9))
            _refresh_collector_status(config, state, first + timedelta(seconds=10))

        self.assertEqual(load.call_count, 2)
        load.assert_called_with(
            now=first + timedelta(seconds=10),
            expected_gpu_count=2,
        )
        self.assertEqual(state.collector_status, payload)
        self.assertEqual(state.collector_checked_at, first + timedelta(seconds=10))

    def test_worker_labels_are_compact_and_defensive(self):
        self.assertEqual(_worker_label({"state": "idle"}), "IDLE")
        self.assertEqual(_worker_label({"state": "running", "running": True}), "OK")
        self.assertEqual(_worker_label({"state": "running", "running": False}), "ERR")
        self.assertEqual(_worker_label({"state": "not-seen"}), "OFF")
        self.assertEqual(_worker_label({"state": "stopped"}), "STOP")
        self.assertEqual(_worker_label({"state": "other-instance"}), "OTHER")
        self.assertEqual(_worker_label({"state": "unverified"}), "UNVER")
        self.assertEqual(_worker_label({"state": "unavailable"}), "N/A")
        self.assertEqual(_worker_label({"state": "future-state"}), "ERR")
        self.assertEqual(_worker_label(None), "ERR")

    def test_worker_status_refresh_is_on_demand_read_only_and_rate_limited(self):
        config = Config(data_dir=Path("/tmp/bk-tui-worker"), gpu_count=2)
        state = TuiState()
        actor = Actor(os.getuid(), "current")
        first = self.start
        pending = reservation("scheduled", actor.uid, MODE_SHARED, [0], first, self.end)
        pending["job"] = {"status": "pending"}
        payload = {"state": "running", "running": True}

        with mock.patch("bk.tui.inspect_worker_status", return_value=payload) as inspect:
            _refresh_worker_status(config, state, [pending], actor, first)
            _refresh_worker_status(
                config,
                state,
                [pending],
                actor,
                first + timedelta(seconds=9),
            )
            _refresh_worker_status(
                config,
                state,
                [pending],
                actor,
                first + timedelta(seconds=10),
            )

        self.assertEqual(inspect.call_count, 2)
        inspect.assert_called_with(config, actor, at=first + timedelta(seconds=10))
        self.assertEqual(state.worker_status, payload)
        self.assertEqual(state.worker_checked_at, first + timedelta(seconds=10))

        with mock.patch("bk.tui.inspect_worker_status") as inspect_idle:
            _refresh_worker_status(config, state, [], actor, first + timedelta(seconds=11))
        inspect_idle.assert_not_called()
        self.assertEqual(state.worker_status, {"state": "idle", "running": None})
        self.assertIsNone(state.worker_checked_at)

    def test_curses_timeout_uses_configured_refresh_interval(self):
        screen = mock.Mock()
        with (
            mock.patch("bk.tui.curses.curs_set"),
            mock.patch("bk.tui.curses.has_colors", return_value=False),
            mock.patch("bk.tui._apply_tui_theme"),
        ):
            _init_curses(screen, 2.5)

        screen.timeout.assert_called_once_with(2500)
        screen.keypad.assert_called_once_with(True)

    def test_refresh_key_invalidates_monitor_and_worker_caches(self):
        state = TuiState(
            collector_checked_at=self.start,
            worker_checked_at=self.start,
        )

        _handle_key(mock.Mock(), ord("r"), self.config, mock.Mock(), state)

        self.assertIsNone(state.collector_checked_at)
        self.assertIsNone(state.worker_checked_at)
        self.assertIn("refreshed now", state.message)
        self.assertFalse(state.error)

    def test_theme_auto_detection_supports_dark_light_and_explicit_override(self):
        self.assertEqual(_resolve_tui_theme("auto", "15;0"), "dark")
        self.assertEqual(_resolve_tui_theme("auto", "0;15"), "light")
        self.assertEqual(_resolve_tui_theme("light", "15;0"), "light")
        self.assertEqual(_resolve_tui_theme("dark", "0;15"), "dark")
        self.assertEqual(_resolve_tui_theme("invalid", "unknown"), "dark")

    def test_dark_and_light_themes_use_distinct_readable_reservation_palettes(self):
        with mock.patch.object(curses, "COLORS", 256, create=True):
            dark_palette = _reservation_palette("dark")
            light_palette = _reservation_palette("light")

        self.assertEqual(len(set(dark_palette)), 8)
        self.assertEqual(len(set(light_palette)), 8)
        self.assertTrue(set(dark_palette).isdisjoint(light_palette))
        self.assertNotEqual(_theme_color_pairs("dark", True), _theme_color_pairs("light", True))

    def test_timeline_defaults_to_six_context_columns_before_now(self):
        now = datetime(2030, 1, 1, 17, 17, 42, tzinfo=timezone.utc)

        start = _default_timeline_view_start(now, 5)

        self.assertEqual(start, datetime(2030, 1, 1, 16, 45, tzinfo=timezone.utc))
        self.assertEqual(_timeline_now_col(now, start, start + timedelta(hours=1), 12), 6)

    def test_add_defaults_to_the_current_five_minute_slot(self):
        now = datetime(2030, 1, 1, 17, 41, 23, tzinfo=timezone.utc)
        state = TuiState()

        with mock.patch("bk.tui.utc_now", return_value=now):
            _start_add_select(self.config, state)
            preview = _build_add_preview({}, self.config, state, state.editor_view_start)

        self.assertEqual(preview.start, datetime(2030, 1, 1, 17, 40, tzinfo=timezone.utc))
        self.assertTrue(preview.valid, preview.reason)

    def test_add_starts_on_an_enabled_gpu_and_rejects_disabled_selection(self):
        config = Config(
            data_dir=Path("/tmp/bk-tui-disabled-test"),
            gpu_count=3,
            disabled_gpus=(0, 2),
        )
        state = TuiState(add_cursor_gpu=0)

        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            _start_add_select(config, state)
            self.assertEqual(state.add_cursor_gpu, 1)
            self.assertEqual(state.add_selected_gpus, {1})

            state.add_cursor_gpu = 2
            _handle_add_key(ord(" "), config, store, state)

        self.assertEqual(state.add_selected_gpus, {1})
        self.assertTrue(state.error)
        self.assertIn("disabled by the administrator", state.message)

    def test_preview_and_gpu_label_expose_administrator_disabled_gpu(self):
        config = Config(
            data_dir=Path("/tmp/bk-tui-disabled-preview"),
            gpu_count=2,
            disabled_gpus=(1,),
        )
        state = self.state(gpus={1})

        preview = _build_add_preview({}, config, state, self.start)
        label = _gpu_label(GpuSnapshot(1, "gpu1"), 24, disabled=True)

        self.assertFalse(preview.valid)
        self.assertIn("disabled by the administrator", preview.reason)
        self.assertIn("OFF", label)

    def test_add_uses_configured_booking_slice_independently_of_timeline_zoom(self):
        now = datetime(2030, 1, 1, 17, 47, 23, tzinfo=timezone.utc)
        config = Config(
            data_dir=Path("/tmp/bk-tui-ten-minute-test"),
            gpu_count=2,
            max_shared_users=2,
            slot_minutes=10,
        )
        state = TuiState()

        with tempfile.TemporaryDirectory() as tmp, mock.patch("bk.tui.utc_now", return_value=now):
            store = LedgerStore(Path(tmp))
            _start_add_select(config, state)
            initial = _build_add_preview({}, config, state, state.editor_view_start)
            _handle_add_key(curses.KEY_RIGHT, config, store, state)
            moved = _build_add_preview({}, config, state, state.editor_view_start)
            _handle_add_key(ord("]"), config, store, state)
            extended = _build_add_preview({}, config, state, state.editor_view_start)

        self.assertEqual(state.slot_minutes, 5)
        self.assertEqual(state.booking_slot_minutes, 10)
        self.assertEqual(initial.start, datetime(2030, 1, 1, 17, 40, tzinfo=timezone.utc))
        self.assertEqual(initial.end - initial.start, timedelta(minutes=30))
        self.assertEqual(moved.start, initial.start + timedelta(minutes=10))
        self.assertEqual(extended.end - extended.start, timedelta(minutes=40))

    def test_non_five_minute_slice_keeps_editor_view_and_preview_aligned(self):
        now = datetime(2030, 1, 1, 17, 41, 23, tzinfo=timezone.utc)
        config = Config(
            data_dir=Path("/tmp/bk-tui-four-minute-test"),
            gpu_count=1,
            slot_minutes=4,
        )
        state = TuiState()

        with mock.patch("bk.tui.utc_now", return_value=now):
            _start_add_select(config, state)
            preview = _build_add_preview({}, config, state, state.editor_view_start)

        self.assertEqual(int(state.editor_view_start.timestamp()) % (4 * 60), 0)
        self.assertEqual(int(preview.start.timestamp()) % (4 * 60), 0)
        self.assertTrue(preview.valid, preview.reason)

    def test_sub_five_minute_booking_slice_is_available_as_a_zoom_level(self):
        config = Config(
            data_dir=Path("/tmp/bk-tui-one-minute-test"),
            gpu_count=1,
            slot_minutes=1,
        )
        state = TuiState()

        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp))
            _start_add_select(config, state)
            _handle_add_key(ord("="), config, store, state)

        self.assertIn(1, state.zoom_levels)
        self.assertEqual(state.slot_minutes, 1)
        self.assertEqual(state.booking_slot_minutes, 1)

    def test_edit_still_rejects_the_already_started_current_slot(self):
        now = datetime(2030, 1, 1, 17, 41, 23, tzinfo=timezone.utc)
        state = self.state()
        state.add_mode = False
        state.edit_mode = True
        state.editor_view_start = datetime(2030, 1, 1, 17, 40, tzinfo=timezone.utc)

        with mock.patch("bk.tui.utc_now", return_value=now):
            preview = _build_add_preview({}, self.config, state, state.editor_view_start)

        self.assertFalse(preview.valid)
        expected = datetime(2030, 1, 1, 17, 45, tzinfo=timezone.utc).astimezone().strftime("%m-%d %H:%M")
        self.assertIn(expected, preview.reason)

    def test_now_marker_and_history_cells_have_distinct_rendering(self):
        start = self.start
        now = start + timedelta(minutes=7)

        current = _decorate_timeline_cell(BAR_CHAR, 12, 0, start + timedelta(minutes=5), start + timedelta(minutes=10), now)
        past = _decorate_timeline_cell(BAR_CHAR, 12, 0, start, start + timedelta(minutes=5), now)
        future = _decorate_timeline_cell(BAR_CHAR, 12, 0, start + timedelta(minutes=10), start + timedelta(minutes=15), now)

        self.assertEqual(current[0], NOW_CHAR)
        self.assertTrue(current[2] & curses.A_BOLD)
        self.assertTrue(past[2] & curses.A_DIM)
        self.assertFalse(future[2] & curses.A_DIM)

    def test_now_label_clears_partial_neighboring_minute_digits(self):
        cleaned, label_col = _clear_now_label_slot("15 30 35 40 45", 7)
        rendered = cleaned[:label_col] + "NOW" + cleaned[label_col + 3 :]

        self.assertEqual(rendered[label_col - 1 : label_col + 4], " NOW ")

    def test_timeline_can_pan_into_history_and_n_returns_to_now(self):
        state = TuiState()
        config = Config(data_dir=Path("/tmp/bk-tui-test"), gpu_count=2, ledger_retention_days=2)

        _pan_timeline(config, state, -1)
        self.assertLess(state.offset_slots, 0)
        self.assertIn("history", state.message)

        with tempfile.TemporaryDirectory() as tmp:
            _handle_key(None, ord("n"), config, LedgerStore(Path(tmp)), state)
        self.assertEqual(state.offset_slots, 0)
        self.assertIn("NOW", state.message)

    def test_indexed_timeline_cells_do_not_reparse_reservation_times(self):
        active = []
        for number in range(200):
            start = self.start + timedelta(minutes=(number * 5) % 600)
            active.append(reservation(str(number), number, MODE_SHARED, [number % 8], start, start + timedelta(hours=1)))
        index = ReservationIndex.from_ledger(self.ledger(active), self.start)

        with mock.patch("bk.tui.parse_iso", wraps=parse_iso) as parser:
            colors = _reservation_color_map(active, index)
            for gpu in range(8):
                for col in range(100):
                    left = self.start + timedelta(minutes=5 * col)
                    _cell_for_gpu(
                        gpu,
                        colors,
                        active,
                        left,
                        left + timedelta(minutes=5),
                        None,
                        col,
                        reservation_index=index,
                    )

        self.assertEqual(parser.call_count, 0)

    def test_preview_allows_shared_overlap_under_record_limit(self):
        existing = reservation("one", os.getuid(), MODE_SHARED, [0], self.start, self.end)

        preview = _build_add_preview(self.ledger([existing]), self.config, self.state(), self.start)

        self.assertTrue(preview.valid, preview.reason)
        self.assertEqual(preview.selected_gpus, (0,))
        self.assertEqual(preview.start, self.start)
        self.assertEqual(preview.end, self.end)

    def test_preview_rejects_shared_overlap_over_record_limit(self):
        existing = [
            reservation("one", os.getuid(), MODE_SHARED, [0], self.start, self.end),
            reservation("two", os.getuid(), MODE_SHARED, [0], self.start, self.end),
        ]

        preview = _build_add_preview(self.ledger(existing), self.config, self.state(), self.start)

        self.assertFalse(preview.valid)
        self.assertTrue(preview.blink)
        self.assertIn("shared capacity full", preview.reason)

    def test_preview_checks_weighted_capacity_and_carries_share_metadata(self):
        config = Config(
            data_dir=Path("/tmp/bk-tui-test"),
            gpu_count=1,
            max_shared_users=4,
        )
        existing = reservation("one", os.getuid(), MODE_SHARED, [0], self.start, self.end)
        existing["share_units"] = 2
        state = self.state()
        state.add_share_units = 2

        valid = _build_add_preview(self.ledger([existing]), config, state, self.start)
        state.add_share_units = 3
        blocked = _build_add_preview(self.ledger([existing]), config, state, self.start)

        self.assertTrue(valid.valid, valid.reason)
        self.assertEqual((valid.share_units, valid.share_capacity), (2, 4))
        self.assertFalse(blocked.valid)
        self.assertIn("projected 5, maximum 4", blocked.reason)

    def test_editor_slot_usage_reports_selected_gpus_and_excludes_edited_booking(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(
                data_dir=Path(tmp),
                gpu_count=2,
                max_shared_users=4,
            )
            store = LedgerStore(config.data_dir)
            existing = add_booking(
                store,
                config,
                BookingRequest(
                    actor=Actor(1001, "alice"),
                    count=1,
                    duration_seconds=30 * 60,
                    start_at=self.start,
                    mode=MODE_SHARED,
                    preferred_gpus=[0],
                    allow_queue=False,
                    share_units=2,
                ),
            ).reservation
            state = TuiState(
                add_mode=True,
                editor_view_start=self.start,
                add_start_steps=0,
                add_duration_steps=6,
                add_selected_gpus={0, 1},
            )

            usage = _editor_shared_slot_usage(config, store, state)
            self.assertEqual(usage, {0: 2, 1: 0})
            self.assertEqual(_editor_slot_usage_text(usage), "used=G0:2,G1:0")

            state.add_mode = False
            state.edit_mode = True
            state.edit_reservation_id = existing["id"]
            self.assertEqual(
                _editor_shared_slot_usage(config, store, state),
                {0: 0, 1: 0},
            )

    def test_preview_rejects_a_start_before_the_current_five_minute_interval(self):
        now = datetime(2030, 1, 1, 17, 2, tzinfo=timezone.utc)
        state = self.state()

        with mock.patch("bk.tui.utc_now", return_value=now):
            preview = _build_add_preview(
                self.ledger([]),
                self.config,
                state,
                now.replace(minute=0) - timedelta(minutes=5),
            )

        self.assertFalse(preview.valid)
        expected = now.replace(minute=0, second=0).astimezone().strftime("%m-%d %H:%M")
        self.assertIn(f"at or after {expected}", preview.reason)

    def test_preview_ignores_ended_legacy_record_but_blocks_a_live_one(self):
        now = self.start + timedelta(minutes=1, seconds=17)
        ended = reservation(
            "ended",
            os.getuid() + 1,
            MODE_EXCLUSIVE,
            [0],
            self.start - timedelta(hours=1),
            self.start + timedelta(seconds=30),
        )
        live = reservation(
            "live",
            os.getuid() + 1,
            MODE_EXCLUSIVE,
            [0],
            self.start - timedelta(hours=1),
            self.start + timedelta(minutes=2),
        )
        state = self.state(mode=MODE_EXCLUSIVE, gpus={0})

        with mock.patch("bk.tui.utc_now", return_value=now):
            ended_preview = _build_add_preview(
                self.ledger([ended]),
                self.config,
                state,
                self.start,
            )
            live_preview = _build_add_preview(
                self.ledger([live]),
                self.config,
                state,
                self.start,
            )

        self.assertTrue(ended_preview.valid, ended_preview.reason)
        self.assertFalse(live_preview.valid)
        self.assertIn("exclusive conflict", live_preview.reason)

    def test_preview_rejects_shared_memory_oversubscription(self):
        existing = reservation("one", os.getuid(), MODE_SHARED, [0], self.start, self.end)
        existing["expected_memory_mb"] = 16 * 1024
        state = self.state()
        state.add_expected_memory_mb = 12 * 1024
        state.gpu_memory_capacity_mb = {0: 24 * 1024}

        preview = _build_add_preview(self.ledger([existing]), self.config, state, self.start)

        self.assertFalse(preview.valid)
        self.assertIn("shared memory full", preview.reason)

    def test_preview_rejects_exclusive_overlap_on_any_selected_gpu(self):
        existing = [reservation("one", os.getuid() + 1, MODE_SHARED, [1], self.start, self.end)]

        preview = _build_add_preview(
            self.ledger(existing),
            self.config,
            self.state(mode=MODE_EXCLUSIVE, gpus={0, 1}),
            self.start,
        )

        self.assertFalse(preview.valid)
        self.assertIn("shared reservations", preview.reason)

    def test_preview_colors_distinguish_modes_and_add_interval_blinks(self):
        shared = AddPreview(self.start, self.end, (0,), 0, MODE_SHARED, True, blink=True)
        exclusive = AddPreview(self.start, self.end, (0,), 0, MODE_EXCLUSIVE, True, blink=True)
        steady_edit = AddPreview(self.start, self.end, (0,), 0, MODE_SHARED, True)

        shared_cell = _preview_cell_for_gpu(0, self.start, self.end, shared)
        exclusive_cell = _preview_cell_for_gpu(0, self.start, self.end, exclusive)
        edit_cell = _preview_cell_for_gpu(0, self.start, self.end, steady_edit)

        self.assertIsNotNone(shared_cell)
        self.assertIsNotNone(exclusive_cell)
        self.assertEqual(shared_cell[1], COLOR_PREVIEW_SHARED)
        self.assertEqual(exclusive_cell[1], COLOR_PREVIEW_EXCLUSIVE)
        self.assertNotEqual(shared_cell[1], exclusive_cell[1])
        self.assertFalse(shared_cell[2] & curses.A_REVERSE)
        self.assertTrue(shared_cell[2] & curses.A_BLINK)
        self.assertFalse(exclusive_cell[2] & curses.A_REVERSE)
        self.assertTrue(exclusive_cell[2] & curses.A_BLINK)
        self.assertFalse(edit_cell[2] & curses.A_BLINK)

    def test_editor_modes_suppress_existing_reservation_selection(self):
        active = [reservation("mine", os.getuid(), MODE_SHARED, [0], self.start, self.end)]

        self.assertIsNone(_timeline_selected_id(active, TuiState()))
        self.assertEqual(_timeline_selected_id(active, TuiState(selected=0)), "mine")
        self.assertIsNone(_timeline_selected_id(active, TuiState(add_mode=True)))
        self.assertIsNone(_timeline_selected_id(active, TuiState(edit_mode=True)))
        self.assertIsNone(_timeline_selected_id(active, TuiState(focus=FOCUS_GPUS)))

    def test_reservation_focus_can_select_another_users_booking(self):
        active = [
            reservation("other", os.getuid() + 1, MODE_SHARED, [0], self.start, self.end),
            reservation("mine", os.getuid(), MODE_SHARED, [1], self.start, self.end),
        ]

        self.assertEqual(_timeline_selected_id(active, TuiState(selected=0)), "other")
        self.assertEqual(_timeline_selected_id(active, TuiState(selected=1)), "mine")

    def test_gpu_focus_navigation_and_add_uses_focused_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=2, max_shared_users=2)
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            add_booking(
                store,
                config,
                BookingRequest(actor, 1, 30 * 60, self.start, MODE_SHARED, [1]),
            )
            state = TuiState(selected=0)

            _toggle_focus(config, store, state)
            self.assertEqual(state.focus, FOCUS_GPUS)
            self.assertEqual(state.selected_gpu, 1)

            _handle_key(None, ord("a"), config, store, state)
            self.assertTrue(state.add_mode)
            self.assertEqual(state.add_cursor_gpu, 1)
            self.assertEqual(state.add_selected_gpus, {1})
            preview = _build_add_preview(store.load(), config, state, state.editor_view_start)
            self.assertTrue(preview.blink)

    def test_delete_key_uses_central_cancellation_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = Config(
                data_dir=root / "data",
                gpu_count=2,
                max_shared_users=2,
                job_log_dir=root / "private-jobs",
            )
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            created = add_booking(
                store,
                config,
                BookingRequest(actor, 1, 30 * 60, self.start, MODE_SHARED, [0]),
            ).reservation
            state = TuiState(selected=0, focus=FOCUS_RESERVATIONS)

            with (
                mock.patch("bk.tui._prompt_line", return_value="yes"),
                mock.patch(
                    "bk.tui.submit_cancellation",
                    wraps=submit_cancellation,
                ) as submit,
            ):
                _handle_key(None, ord("d"), config, store, state)

            submit.assert_called_once_with(config, store, mock.ANY, created["id"])
            stored = next(
                item for item in store.load()["reservations"] if item["id"] == created["id"]
            )
            self.assertEqual(stored["status"], "cancelled")
            self.assertIn("deleted", state.message)

    def test_add_auto_find_switches_to_an_available_gpu_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=2, max_shared_users=1)
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            add_booking(
                store,
                config,
                BookingRequest(actor, 1, 30 * 60, self.start, MODE_EXCLUSIVE, [0]),
            )
            state = self.state(mode=MODE_EXCLUSIVE, gpus={0})
            state.editor_view_start = self.start

            _handle_add_key(ord("f"), config, store, state)

            self.assertEqual(state.add_selected_gpus, {1})
            self.assertEqual(state.add_cursor_gpu, 1)
            preview = _build_add_preview(store.load(), config, state, state.editor_view_start)
            view_end = state.editor_view_start + timedelta(minutes=state.timeline_columns * state.slot_minutes)
            self.assertEqual(preview.start, self.start)
            self.assertLessEqual(state.editor_view_start, utc_now())
            self.assertLess(preview.end, view_end)
            self.assertFalse(state.error)
            self.assertIn("earliest found 1 GPU [1]", state.message)
            self.assertEqual(len(store.load()["reservations"]), 1)

    def test_add_auto_find_preserves_the_selected_gpu_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=3, max_shared_users=1)
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            add_booking(
                store,
                config,
                BookingRequest(actor, 1, 30 * 60, self.start, MODE_EXCLUSIVE, [0]),
            )
            state = self.state(mode=MODE_EXCLUSIVE, gpus={0, 1})
            state.editor_view_start = self.start

            _handle_add_key(ord("f"), config, store, state)

            self.assertEqual(state.add_selected_gpus, {1, 2})
            preview = _build_add_preview(store.load(), config, state, state.editor_view_start)
            self.assertEqual(preview.start, self.start)
            self.assertGreaterEqual(state.slot_minutes, 30)
            self.assertIn("earliest found 2 GPU [1,2]", state.message)
            self.assertEqual(len(store.load()["reservations"]), 1)

    def test_add_fixed_find_keeps_selected_gpu_and_jumps_to_its_next_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=2, max_shared_users=1)
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            add_booking(
                store,
                config,
                BookingRequest(actor, 1, 45 * 60, self.start, MODE_EXCLUSIVE, [0]),
            )
            state = self.state(mode=MODE_EXCLUSIVE, gpus={0}, duration_steps=6)
            state.editor_view_start = self.start

            _handle_add_key(ord("g"), config, store, state)

            self.assertEqual(state.add_selected_gpus, {0})
            preview = _build_add_preview(store.load(), config, state, state.editor_view_start)
            view_end = state.editor_view_start + timedelta(minutes=state.timeline_columns * state.slot_minutes)
            self.assertEqual(preview.start, self.start + timedelta(minutes=45))
            self.assertTrue(preview.valid, preview.reason)
            self.assertLessEqual(state.editor_view_start, utc_now())
            self.assertLess(preview.end, view_end)
            self.assertIn("fixed earliest found 1 GPU [0]", state.message)
            self.assertEqual(len(store.load()["reservations"]), 1)

    def test_add_fixed_find_requires_a_gpu_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=2, max_shared_users=1)
            store = LedgerStore(config.data_dir)
            state = self.state(gpus=set())
            state.editor_view_start = self.start

            _handle_add_key(ord("g"), config, store, state)

            self.assertTrue(state.error)
            self.assertIn("select at least one GPU", state.message)
            self.assertEqual(store.load()["reservations"], [])

    def test_manual_adjustment_clears_auto_find_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=1, max_shared_users=1)
            store = LedgerStore(config.data_dir)
            state = self.state()
            state.editor_view_start = self.start
            state.message = "auto found"

            _handle_add_key(curses.KEY_RIGHT, config, store, state)

            self.assertEqual(state.message, "")
            self.assertFalse(state.error)

    def test_number_key_sets_gpu_count_and_finds_earliest_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=3, max_shared_users=1)
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            add_booking(
                store,
                config,
                BookingRequest(actor, 1, 30 * 60, self.start, MODE_EXCLUSIVE, [0]),
            )
            state = self.state(mode=MODE_EXCLUSIVE, gpus={0})
            state.editor_view_start = self.start

            _handle_add_key(ord("2"), config, store, state)

            self.assertEqual(state.add_selected_gpus, {1, 2})
            preview = _build_add_preview(store.load(), config, state, state.editor_view_start)
            self.assertEqual(preview.start, self.start)
            self.assertIn("earliest found 2 GPU [1,2]", state.message)

    def test_earliest_search_keeps_now_anchor_after_an_automatic_jump(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=1, max_shared_users=1)
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            add_booking(
                store,
                config,
                BookingRequest(
                    actor,
                    1,
                    30 * 60,
                    self.start + timedelta(minutes=30),
                    MODE_EXCLUSIVE,
                    [0],
                ),
            )
            state = self.state(mode=MODE_EXCLUSIVE, duration_steps=12)
            state.editor_view_start = self.start
            state.add_search_anchor = self.start

            _handle_add_key(ord("f"), config, store, state)
            first = _build_add_preview(store.load(), config, state, state.editor_view_start)
            self.assertEqual(first.start, self.start + timedelta(hours=1))
            self.assertEqual(state.add_search_anchor, self.start)

            state.add_duration_steps = 6
            _handle_add_key(ord("f"), config, store, state)
            second = _build_add_preview(store.load(), config, state, state.editor_view_start)

            self.assertEqual(second.start, self.start)
            self.assertEqual(state.add_search_anchor, self.start)

    def test_manual_time_move_sets_the_earliest_search_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=1, max_shared_users=1)
            store = LedgerStore(config.data_dir)
            state = self.state(mode=MODE_EXCLUSIVE)
            state.editor_view_start = self.start
            state.add_search_anchor = self.start

            for _ in range(6):
                _handle_add_key(curses.KEY_RIGHT, config, store, state)
            _handle_add_key(ord("f"), config, store, state)

            preview = _build_add_preview(store.load(), config, state, state.editor_view_start)
            self.assertEqual(preview.start, self.start + timedelta(minutes=30))
            self.assertEqual(state.add_search_anchor, self.start + timedelta(minutes=30))

    def test_nearest_search_is_a_separate_key_and_prefers_the_left_tie(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=1, max_shared_users=1)
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            target = self.start + timedelta(hours=1)
            add_booking(
                store,
                config,
                BookingRequest(actor, 1, 30 * 60, target, MODE_EXCLUSIVE, [0]),
            )
            state = self.state(mode=MODE_EXCLUSIVE)
            state.editor_view_start = target
            state.add_search_anchor = target

            _handle_add_key(ord("o"), config, store, state)

            preview = _build_add_preview(store.load(), config, state, state.editor_view_start)
            self.assertEqual(preview.start, target - timedelta(minutes=30))
            self.assertIn("nearest found", state.message)

    def test_editor_has_fine_quick_and_zoom_adjustments(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=1)
            store = LedgerStore(config.data_dir)
            state = self.state(duration_steps=6)

            _handle_add_key(ord("]"), config, store, state)
            self.assertEqual(state.add_duration_steps, 7)
            _handle_add_key(ord("["), config, store, state)
            self.assertEqual(state.add_duration_steps, 6)
            _handle_add_key(ord("."), config, store, state)
            self.assertEqual(state.add_duration_steps, 12)
            _handle_add_key(ord(","), config, store, state)
            self.assertEqual(state.add_duration_steps, 6)
            _handle_add_key(ord("-"), config, store, state)
            self.assertEqual(state.slot_minutes, 10)
            self.assertEqual(state.add_duration_steps, 6)
            _handle_add_key(ord("="), config, store, state)
            self.assertEqual(state.slot_minutes, 5)

    def test_speed_key_accelerates_time_and_duration_without_shift_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=8)
            store = LedgerStore(config.data_dir)
            state = self.state(duration_steps=6)

            _handle_add_key(ord("v"), config, store, state)
            _handle_add_key(curses.KEY_RIGHT, config, store, state)
            _handle_add_key(ord("]"), config, store, state)

            self.assertEqual(state.speed_multiplier, 6)
            self.assertEqual(state.add_start_steps, 6)
            self.assertEqual(state.add_duration_steps, 12)

    def test_memory_key_without_live_screen_explains_how_to_enter_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=1)
            store = LedgerStore(config.data_dir)
            state = self.state()

            _handle_add_key(ord("m"), config, store, state)

            self.assertIn("live TUI", state.message)
            self.assertFalse(state.error)

    def test_arrow_navigation_moves_between_gpu_and_reservation_focus(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=4, max_shared_users=2)
            store = LedgerStore(config.data_dir)
            state = TuiState(focus=FOCUS_RESERVATIONS, selected=0)

            _move_focus_up(config, store, state)
            self.assertEqual(state.focus, FOCUS_RESERVATIONS)
            self.assertEqual(state.selected, -1)
            _move_focus_up(config, store, state)
            self.assertEqual(state.focus, FOCUS_GPUS)
            self.assertEqual(state.selected_gpu, 3)

            _move_focus_down(config, store, state)
            self.assertEqual(state.focus, FOCUS_RESERVATIONS)
            self.assertEqual(state.selected, -1)

    def test_gpu_focus_detail_lists_only_visible_shared_reservations(self):
        visible = reservation("visible", os.getuid(), MODE_SHARED, [0], self.start, self.end)
        other_gpu = reservation("other", os.getuid(), MODE_SHARED, [1], self.start, self.end)
        exclusive = reservation("exclusive", os.getuid(), MODE_EXCLUSIVE, [0], self.start, self.end)
        later = reservation(
            "later",
            os.getuid(),
            MODE_SHARED,
            [0],
            self.end,
            self.end + timedelta(minutes=30),
        )

        detail = _visible_shared_reservations(
            [visible, other_gpu, exclusive, later],
            0,
            self.start,
            self.end,
        )

        self.assertEqual([item["id"] for item in detail], ["visible"])

    def test_edit_preview_ignores_the_reservation_being_edited(self):
        config = Config(data_dir=Path("/tmp/bk-tui-test"), gpu_count=1, max_shared_users=1)
        existing = reservation("mine", os.getuid(), MODE_SHARED, [0], self.start, self.end)
        state = TuiState(
            edit_mode=True,
            edit_reservation_id="mine",
            add_cursor_gpu=0,
            add_start_steps=0,
            add_duration_steps=6,
            add_selected_gpus={0},
            add_booking_mode=MODE_SHARED,
        )

        preview = _build_add_preview(self.ledger([existing]), config, state, self.start)

        self.assertTrue(preview.valid, preview.reason)

    def test_edit_state_loads_existing_time_gpus_and_mode(self):
        existing = reservation(
            "mine",
            os.getuid(),
            MODE_EXCLUSIVE,
            [1],
            self.start,
            self.start + timedelta(minutes=45),
        )
        state = TuiState(add_mode=True)

        _load_edit_state(self.config, state, existing)

        self.assertFalse(state.add_mode)
        self.assertTrue(state.edit_mode)
        self.assertEqual(state.edit_reservation_id, "mine")
        self.assertEqual(state.editor_view_start, self.start - timedelta(minutes=30))
        self.assertEqual(state.add_start_steps, 6)
        self.assertEqual(state.add_duration_steps, 9)
        self.assertEqual(state.add_selected_gpus, {1})
        self.assertEqual(state.add_cursor_gpu, 1)
        self.assertEqual(state.add_booking_mode, MODE_EXCLUSIVE)

    def test_started_reservation_cannot_enter_timeline_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=1)
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            created = add_booking(
                store,
                config,
                BookingRequest(actor, 1, 30 * 60, self.start, MODE_SHARED, [0]),
            )
            state = TuiState(selected=_own_reservation_index(store, created.reservation["id"]))

            with mock.patch("bk.tui.utc_now", return_value=self.start + timedelta(minutes=1)):
                _start_edit_select(config, store, state)

        self.assertFalse(state.editor_active)
        self.assertTrue(state.error)
        self.assertIn("after it has started", state.message)

    def test_edit_auto_find_excludes_original_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=1, max_shared_users=1)
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            created = add_booking(
                store,
                config,
                BookingRequest(actor, 1, 30 * 60, self.start, MODE_EXCLUSIVE, [0]),
            )
            state = TuiState(selected=_own_reservation_index(store, created.reservation["id"]))
            _start_edit_select(config, store, state)

            _handle_add_key(ord("f"), config, store, state)

            preview = _build_add_preview(store.load(), config, state, state.editor_view_start)
            self.assertEqual(preview.start, self.start)
            self.assertEqual(preview.selected_gpus, (0,))
            self.assertTrue(preview.valid, preview.reason)
            self.assertIn("earliest found 1 GPU [0]", state.message)

    def test_edit_reset_restores_original_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=2, max_shared_users=2)
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            created = add_booking(
                store,
                config,
                BookingRequest(actor, 1, 30 * 60, self.start, MODE_SHARED, [0]),
            )
            state = TuiState(selected=_own_reservation_index(store, created.reservation["id"]))
            _start_edit_select(config, store, state)
            _handle_add_key(curses.KEY_RIGHT, config, store, state)
            _handle_add_key(ord("]"), config, store, state)
            _handle_add_key(ord("x"), config, store, state)

            _handle_add_key(ord("r"), config, store, state)

            preview = _build_add_preview(store.load(), config, state, state.editor_view_start)
            self.assertEqual(preview.start, self.start)
            self.assertEqual(preview.end, self.start + timedelta(minutes=30))
            self.assertEqual(preview.mode, MODE_SHARED)
            self.assertEqual(preview.selected_gpus, (0,))
            self.assertEqual(state.message, "edit reset to original")

    def test_timeline_edit_submits_exact_updated_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=2, max_shared_users=2)
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            created = add_booking(
                store,
                config,
                BookingRequest(
                    actor=actor,
                    count=1,
                    duration_seconds=30 * 60,
                    start_at=self.start,
                    mode=MODE_SHARED,
                    preferred_gpus=[0],
                ),
            )
            state = TuiState(selected=_own_reservation_index(store, created.reservation["id"]))
            _start_edit_select(config, store, state)

            _handle_add_key(curses.KEY_RIGHT, config, store, state)
            _handle_add_key(ord("]"), config, store, state)
            _handle_add_key(ord(" "), config, store, state)
            _handle_add_key(curses.KEY_DOWN, config, store, state)
            _handle_add_key(ord(" "), config, store, state)
            _handle_add_key(ord("x"), config, store, state)
            _handle_add_key(10, config, store, state)

            updated = next(
                item
                for item in store.load()["reservations"]
                if item["id"] == created.reservation["id"]
            )
            self.assertFalse(state.editor_active)
            self.assertEqual(state.message, f"updated {created.reservation['id'][:6]}")
            self.assertEqual(updated["mode"], MODE_EXCLUSIVE)
            self.assertEqual(updated["gpus"], [1])
            self.assertEqual(updated["start_at"], iso(self.start + timedelta(minutes=5)))
            self.assertEqual(updated["end_at"], iso(self.start + timedelta(minutes=40)))

    def test_conflicting_timeline_edit_stays_open_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=Path(tmp), gpu_count=2, max_shared_users=2)
            store = LedgerStore(config.data_dir)
            actor = Actor(uid=os.getuid(), username="current")
            target = add_booking(
                store,
                config,
                BookingRequest(actor, 1, 30 * 60, self.start, MODE_SHARED, [0]),
            )
            add_booking(
                store,
                config,
                BookingRequest(actor, 1, 30 * 60, self.start, MODE_EXCLUSIVE, [1]),
            )
            state = TuiState(selected=_own_reservation_index(store, target.reservation["id"]))
            _start_edit_select(config, store, state)

            _handle_add_key(ord(" "), config, store, state)
            _handle_add_key(curses.KEY_DOWN, config, store, state)
            _handle_add_key(ord(" "), config, store, state)
            _handle_add_key(ord("x"), config, store, state)
            _handle_add_key(10, config, store, state)

            original = next(
                item
                for item in store.load()["reservations"]
                if item["id"] == target.reservation["id"]
            )
            self.assertTrue(state.edit_mode)
            self.assertTrue(state.error)
            self.assertIn("conflict", state.message)
            self.assertEqual(original["mode"], MODE_SHARED)
            self.assertEqual(original["gpus"], [0])

    def test_capacity_text_reports_this_reservations_share(self):
        active = [
            reservation("one", os.getuid(), MODE_SHARED, [0], self.start, self.end),
            reservation("two", os.getuid(), MODE_SHARED, [0], self.start, self.end),
            reservation("exclusive", os.getuid(), MODE_EXCLUSIVE, [1], self.start, self.end),
        ]

        self.assertEqual(_capacity_text(active[0], active, 2), "1/2")
        self.assertEqual(_capacity_text(active[2], active, 2), "-")

    def test_two_shared_reservations_split_gpu_band_into_vertical_lanes(self):
        active = [
            reservation("one", os.getuid(), MODE_SHARED, [0], self.start, self.end),
            reservation("two", os.getuid(), MODE_SHARED, [0], self.start, self.end),
        ]

        color_map = _reservation_color_map(active)
        top_lane = _cell_for_gpu(0, color_map, active, self.start, self.start + timedelta(minutes=5), "two", 0, 0, 4)
        bottom_lane = _cell_for_gpu(0, color_map, active, self.start, self.start + timedelta(minutes=5), "two", 0, 3, 4)

        self.assertEqual(top_lane[0], BAR_CHAR)
        self.assertEqual(bottom_lane[0], BAR_CHAR)
        self.assertEqual(top_lane[1], color_map["one"])
        self.assertEqual(bottom_lane[1], color_map["two"])
        self.assertFalse(top_lane[2] & curses.A_BLINK)
        self.assertTrue(bottom_lane[2] & curses.A_BLINK)

    def test_four_shared_reservations_can_use_four_distinct_vertical_lanes(self):
        active = [
            reservation("a", os.getuid(), MODE_SHARED, [0], self.start, self.end),
            reservation("b", os.getuid() + 1, MODE_SHARED, [0], self.start, self.end),
            reservation("c", os.getuid() + 2, MODE_SHARED, [0], self.start, self.end),
            reservation("d", os.getuid() + 3, MODE_SHARED, [0], self.start, self.end),
        ]

        color_map = _reservation_color_map(active)
        cells = [
            _cell_for_gpu(0, color_map, active, self.start, self.start + timedelta(minutes=5), "c", 0, lane, 4)
            for lane in range(4)
        ]

        self.assertTrue(all(cell[0] == BAR_CHAR for cell in cells))
        self.assertEqual([cell[1] for cell in cells], [color_map["a"], color_map["b"], color_map["c"], color_map["d"]])
        self.assertFalse(cells[0][2] & curses.A_BLINK)
        self.assertTrue(cells[2][2] & curses.A_BLINK)

    def test_compact_row_marks_selected_share_inside_the_woven_capacity(self):
        active = [
            reservation("a", os.getuid(), MODE_SHARED, [0], self.start, self.end),
            reservation("b", os.getuid() + 1, MODE_SHARED, [0], self.start, self.end),
            reservation("c", os.getuid() + 2, MODE_SHARED, [0], self.start, self.end),
        ]

        cell = _cell_for_gpu(
            0,
            _reservation_color_map(active),
            active,
            self.start,
            self.start + timedelta(minutes=5),
            "b",
        )

        self.assertEqual(cell[0], WEAVE_CHARS[0])
        self.assertTrue(cell[2] & curses.A_BLINK)

    def test_compact_row_weaves_three_shared_reservations_with_equal_color_pairs(self):
        active = [
            reservation("a", os.getuid(), MODE_SHARED, [0], self.start, self.end),
            reservation("b", os.getuid() + 1, MODE_SHARED, [0], self.start, self.end),
            reservation("c", os.getuid() + 2, MODE_SHARED, [0], self.start, self.end),
        ]
        color_map = _reservation_color_map(active)
        old_pairs = dict(MIXED_COLOR_PAIRS)
        pair_ids = {}
        for index, (left, right) in enumerate(
            (_shared_weave_pair(active, col) for col in range(3)),
            start=90,
        ):
            key = tuple(sorted((color_map[left["id"]], color_map[right["id"]])))
            pair_ids[key] = index
        MIXED_COLOR_PAIRS.update(pair_ids)
        try:
            cells = [
                _cell_for_gpu(
                    0,
                    color_map,
                    active,
                    self.start,
                    self.start + timedelta(minutes=5),
                    "b",
                    col=col,
                )
                for col in range(3)
            ]
        finally:
            MIXED_COLOR_PAIRS.clear()
            MIXED_COLOR_PAIRS.update(old_pairs)

        self.assertEqual([cell[0] for cell in cells], [SPLIT_CHAR, SPLIT_CHAR, SPLIT_CHAR])
        self.assertTrue(cells[0][2] & curses.A_BLINK)
        self.assertFalse(cells[1][2] & curses.A_BLINK)
        self.assertTrue(cells[2][2] & curses.A_BLINK)

    def test_weighted_shares_receive_proportional_subcells(self):
        large = reservation("large", os.getuid(), MODE_SHARED, [0], self.start, self.end)
        small = reservation("small", os.getuid() + 1, MODE_SHARED, [0], self.start, self.end)
        large["share_units"] = 3
        small["share_units"] = 1
        active = [large, small]
        color_map = _reservation_color_map(active)
        pair_key = tuple(sorted((color_map["large"], color_map["small"])))
        old_pairs = dict(MIXED_COLOR_PAIRS)
        MIXED_COLOR_PAIRS[pair_key] = 99
        try:
            first = _cell_for_gpu(
                0,
                color_map,
                active,
                self.start,
                self.start + timedelta(minutes=5),
                "small",
                col=0,
                shared_limit=4,
            )
            second = _cell_for_gpu(
                0,
                color_map,
                active,
                self.start,
                self.start + timedelta(minutes=5),
                "small",
                col=1,
                shared_limit=4,
            )
        finally:
            MIXED_COLOR_PAIRS.clear()
            MIXED_COLOR_PAIRS.update(old_pairs)

        self.assertEqual(first[:2], (SPLIT_CHAR, 99))
        self.assertTrue(first[2] & curses.A_BLINK)
        self.assertEqual(second[:2], (BAR_CHAR, color_map["large"]))
        self.assertFalse(second[2] & curses.A_BLINK)
        self.assertEqual(_capacity_text(large, active, 4), "3/4")

    def test_solid_shared_style_uses_full_bar_until_another_booking_overlaps(self):
        one = reservation("one", os.getuid(), MODE_SHARED, [0], self.start, self.end)
        one["share_units"] = 1
        color_map = _reservation_color_map([one])

        cell = _cell_for_gpu(
            0,
            color_map,
            [one],
            self.start,
            self.start + timedelta(minutes=5),
            None,
            shared_limit=4,
            shared_style="solid",
        )

        self.assertEqual(cell[:2], (BAR_CHAR, color_map["one"]))

    def test_shared_weave_gives_each_reservation_equal_area_per_period(self):
        for count in (3, 4, 5, 6, 8):
            items = [{"id": str(index)} for index in range(count)]
            period = count if count % 2 else count // 2
            appearances = {item["id"]: 0 for item in items}
            for col in range(period):
                top, bottom = _shared_weave_pair(items, col)
                appearances[top["id"]] += 1
                appearances[bottom["id"]] += 1

            self.assertEqual(len(set(appearances.values())), 1, (count, appearances))

    def test_two_shared_reservations_use_split_color_cell_when_available(self):
        active = [
            reservation("one", os.getuid(), MODE_SHARED, [0], self.start, self.end),
            reservation("two", os.getuid() + 1, MODE_SHARED, [0], self.start, self.end),
        ]
        color_map = _reservation_color_map(active)
        key = tuple(sorted((color_map["one"], color_map["two"])))
        old_pairs = dict(MIXED_COLOR_PAIRS)
        MIXED_COLOR_PAIRS[key] = 99
        try:
            cell = _cell_for_gpu(0, color_map, active, self.start, self.start + timedelta(minutes=5), "one")
        finally:
            MIXED_COLOR_PAIRS.clear()
            MIXED_COLOR_PAIRS.update(old_pairs)

        self.assertEqual(cell[0], SPLIT_CHAR)
        self.assertEqual(cell[1], 99)
        self.assertTrue(cell[2] & curses.A_BLINK)

    def test_non_overlapping_reservations_rotate_colors(self):
        active = [
            reservation(f"r{index}", os.getuid() + index, MODE_SHARED, [0], self.start + timedelta(hours=index), self.end + timedelta(hours=index))
            for index in range(4)
        ]

        color_map = _reservation_color_map(active)

        self.assertEqual(len(set(color_map.values())), 4)

    def test_eight_overlapping_reservations_have_distinct_logical_colors(self):
        active = [
            reservation(str(index), os.getuid() + index, MODE_SHARED, [0], self.start, self.end)
            for index in range(8)
        ]

        color_map = _reservation_color_map(active)

        self.assertEqual(len(set(color_map.values())), 8)

    def test_axis_labels_use_hour_suffix_and_plain_minutes(self):
        value = self.start.replace(hour=17, minute=15)

        self.assertEqual(_hour_label(value), "17h")
        self.assertEqual(_minute_label(value), "15")
        self.assertEqual(_weekday_label(datetime(2030, 1, 1)), "Tue")
        self.assertEqual(_date_label(datetime(2030, 1, 1)), "01-01 Tue")

    def test_axis_draws_quarter_hour_labels_and_ruler(self):
        local_tz = datetime.now().astimezone().tzinfo
        start = datetime(2030, 1, 1, 17, 0, tzinfo=local_tz)
        dates, hours, minutes, ruler = _time_axis_lines(start, start + timedelta(minutes=125), 25)

        self.assertEqual(dates[0:9], "01-01 Tue")
        self.assertEqual(hours[0:3], "17h")
        self.assertEqual(hours[12:15], "18h")
        self.assertEqual(minutes[3:5], "15")
        self.assertEqual(minutes[6:8], "30")
        self.assertEqual(minutes[9:11], "45")
        self.assertEqual(ruler[0], "╋")
        self.assertEqual(ruler[3], "┬")
        self.assertEqual(ruler[6], "┿")

    def test_axis_keeps_first_quarter_tick_when_view_starts_near_it(self):
        local_tz = datetime.now().astimezone().tzinfo
        start = datetime(2030, 1, 1, 17, 25, tzinfo=local_tz)
        dates, hours, minutes, ruler = _time_axis_lines(start, start + timedelta(minutes=100), 20)

        self.assertEqual(dates[0:9], "01-01 Tue")
        self.assertEqual(hours[0:3], "17h")
        self.assertEqual(minutes[1:3], "30")
        self.assertNotEqual(minutes[0:2], "25")
        self.assertEqual(ruler[1], "┿")

    def test_axis_marks_new_date_and_weekday_at_midnight(self):
        local_tz = datetime.now().astimezone().tzinfo
        start = datetime(2030, 1, 1, 23, 30, tzinfo=local_tz)

        dates, hours, _minutes, ruler = _time_axis_lines(start, start + timedelta(minutes=120), 24)

        self.assertEqual(dates[6:15], "01-02 Wed")
        self.assertEqual(hours[6:8], "0h")
        self.assertEqual(ruler[6], "╋")

    def test_editor_banner_summarizes_mode_count_date_and_duration(self):
        local_tz = datetime.now().astimezone().tzinfo
        start = datetime(2030, 1, 1, 17, 30, tzinfo=local_tz)
        preview = AddPreview(start, start + timedelta(minutes=45), (0, 2), 0, MODE_EXCLUSIVE, True, blink=True)

        add_text = _editor_banner_text(TuiState(add_mode=True), preview)
        edit_text = _editor_banner_text(
            TuiState(edit_mode=True, edit_reservation_id="abcdef123456"),
            preview,
        )

        self.assertIn("ADD X", add_text)
        self.assertIn("2 GPU [0,2]", add_text)
        self.assertIn("Tue 01-01 17:30->18:15", add_text)
        self.assertIn("45m", add_text)
        self.assertIn("EDIT abcdef X", edit_text)

    def test_visible_ids_use_six_characters_until_a_collision_requires_more(self):
        normal = [{"id": "abcdef111111"}, {"id": "123456222222"}]
        collision = [{"id": "abcdef111111"}, {"id": "abcdef222222"}]

        self.assertEqual(_visible_id_width(normal), 6)
        self.assertEqual(_visible_id_width(collision), 7)

    def test_long_duration_shows_total_hours_and_day_breakdown(self):
        duration = timedelta(days=5, hours=4, minutes=20)

        self.assertEqual(_duration_text(duration), "124h20m")
        self.assertEqual(_duration_detail_text(duration), "124h20m (5d4h20m)")

    def test_gpu_label_is_compact_and_shows_shared_peak(self):
        label = _gpu_label(GpuSnapshot(index=0, name="unknown"), 30, peak_shared=4, shared_limit=4)
        narrow = _gpu_label(GpuSnapshot(index=0, name="unknown"), 20, peak_shared=4, shared_limit=4)

        self.assertEqual(label.split()[0], "0")
        self.assertIn("4/4", label)
        self.assertIn("4/4", narrow)
        self.assertNotIn("unknown", label)

    def test_compact_gpu_header_aligns_with_row_metrics(self):
        gpu = GpuSnapshot(
            index=7,
            name="Sim Pro 6000",
            memory_used_mb=4096,
            memory_total_mb=98304,
            utilization_percent=72,
        )
        width = _timeline_label_width(80)
        header = _gpu_metrics_header(width)
        row = _gpu_row_label(gpu, width, peak_shared=2, shared_limit=4)

        self.assertEqual(width, 21)
        self.assertEqual(len(header), width)
        self.assertEqual(header.index("GPU") + len("GPU"), row.index("7") + len("7"))
        self.assertEqual(header.index("Cap") + len("Cap"), row.index("2/4") + len("2/4"))
        self.assertEqual(header.index("Util") + len("Util"), row.index("72%") + len("72%"))
        self.assertEqual(header.index("Free") + len("Free"), row.index("92.0G") + len("92.0G"))

    def test_gpu_label_compacts_three_digit_free_memory(self):
        gpu = GpuSnapshot(
            index=0,
            name="large-memory",
            memory_used_mb=1024,
            memory_total_mb=145 * 1024,
            utilization_percent=100,
        )

        label = _gpu_label(gpu, 20, peak_shared=4, shared_limit=4)

        self.assertEqual(len(label), 20)
        self.assertIn("144G", label)
        self.assertIn("100%", label)

    def test_gpu_metric_columns_do_not_move_when_shared_capacity_changes(self):
        gpu = GpuSnapshot(
            index=0,
            name="Sim Pro 6000",
            memory_used_mb=4096,
            memory_total_mb=98304,
            utilization_percent=72,
            source="simulation",
        )

        idle = _gpu_label(gpu, 32, peak_shared=0, shared_limit=2)
        shared = _gpu_label(gpu, 32, peak_shared=2, shared_limit=2)

        self.assertIn("0/2", idle)
        self.assertIn("2/2", shared)
        self.assertEqual(idle.split()[1], "0/2")
        self.assertEqual(shared.split()[1], "2/2")
        self.assertEqual(idle.index("72%"), shared.index("72%"))
        self.assertEqual(idle.index("92.0G"), shared.index("92.0G"))

    def test_gpu_label_marks_current_exclusive_without_moving_metrics(self):
        gpu = GpuSnapshot(
            index=2,
            name="Sim Pro 6000",
            memory_used_mb=4096,
            memory_total_mb=98304,
            utilization_percent=72,
            source="simulation",
        )

        shared = _gpu_label(gpu, 32, peak_shared=2, shared_limit=4)
        exclusive = _gpu_label(gpu, 32, shared_limit=4, exclusive=True)

        self.assertIn("X", exclusive)
        self.assertNotIn("of4", exclusive)
        self.assertEqual(shared.index("72%"), exclusive.index("72%"))
        self.assertEqual(shared.index("92.0G"), exclusive.index("92.0G"))

    def test_gpu_label_shows_live_utilization_processes_and_violations(self):
        process = GpuProcessSnapshot(123, 1001, "alice", "python train.py")
        gpu = GpuSnapshot(
            index=0,
            name="Sim Pro 6000",
            memory_used_mb=4096,
            memory_total_mb=98304,
            utilization_percent=72,
            temperature_c=61,
            processes=(process,),
            source="simulation",
        )

        label = _gpu_label(gpu, 32, peak_shared=2, shared_limit=4, violations=1)

        self.assertIn("!1", label)
        self.assertIn("2/4", label)
        self.assertNotIn("of4", label)
        self.assertIn("72%", label)
        self.assertIn("92.0G", label)
        self.assertIn("61C", label)
        self.assertIn("P1", label)

    def test_gpu_label_never_renders_a_partial_trailing_metric(self):
        gpu = GpuSnapshot(
            index=0,
            name="Sim Pro 6000",
            memory_used_mb=512,
            memory_total_mb=32607,
            utilization_percent=0,
            temperature_c=26,
            source="simulation",
        )

        label = _gpu_label(gpu, 30, peak_shared=0, shared_limit=2)
        narrower = _gpu_label(gpu, 20, peak_shared=0, shared_limit=2)

        self.assertIn("31.3G", label)
        self.assertIn("26C", label)
        self.assertNotIn("26C", narrower)
        self.assertFalse(narrower.rstrip().endswith("2"), narrower)

    def test_gpu_focus_marker_uses_a_reserved_column_without_shifting_metrics(self):
        gpu = GpuSnapshot(
            index=7,
            name="sim",
            memory_used_mb=1024,
            memory_total_mb=32768,
            utilization_percent=8,
        )
        width = _timeline_label_width(120)

        plain = _gpu_row_label(gpu, width, peak_shared=1, shared_limit=2, focused=False)
        focused = _gpu_row_label(gpu, width, peak_shared=1, shared_limit=2, focused=True)

        self.assertEqual(len(plain), width)
        self.assertEqual(len(focused), width)
        self.assertEqual(plain[1:], focused[1:])
        self.assertEqual(plain[0], " ")
        self.assertEqual(focused[0], ">")

    def test_reservation_gpu_column_uses_positioned_gpu_numbers(self):
        header = _table_header(120, gpu_count=8)
        gpu_map = _reservation_gpu_text([0, 3, 7], gpu_count=8, width=8)

        self.assertIn(" GPU      ", header)
        self.assertNotIn("01234567", header)
        self.assertEqual(gpu_map, "0  3   7")

    def test_positioned_gpu_numbers_support_ten_gpus_then_fall_back_to_a_list(self):
        self.assertEqual(_reservation_gpu_text([0, 9], gpu_count=10, width=10), "0        9")
        self.assertEqual(_reservation_gpu_text([0, 10], gpu_count=11, width=12), "0,10")

    def test_process_table_line_contains_user_utilization_state_and_command(self):
        process = GpuProcessSnapshot(4321, 1001, "alice", "python train.py", 3072, 68, "C")
        authorized = ProcessUsage(0, process, USAGE_AUTHORIZED, ("booking-123",))
        violation = ProcessUsage(0, process, USAGE_UNRESERVED)

        wide = _process_table_line(authorized, 120)
        compact = _process_table_line(violation, 80)

        self.assertIn("alice", wide)
        self.assertIn("68%", wide)
        self.assertIn("bookin", wide)
        self.assertIn("python train.py", wide)
        self.assertIn("unreserved", compact)

    def test_share_detail_chooses_gpu_with_most_related_reservations(self):
        selected = reservation("selected", os.getuid(), MODE_SHARED, [0, 1], self.start, self.end)
        active = [
            selected,
            reservation("gpu0-peer", os.getuid() + 1, MODE_SHARED, [0], self.start, self.end),
            reservation("gpu1-peer-a", os.getuid() + 2, MODE_SHARED, [1], self.start, self.end),
            reservation("gpu1-peer-b", os.getuid() + 3, MODE_SHARED, [1], self.start, self.end),
        ]

        detail = _selected_share_detail(active, selected, self.start, self.end)

        self.assertIsNotNone(detail)
        gpu, related = detail
        self.assertEqual(gpu, 1)
        self.assertEqual(related[0]["id"], "selected")
        self.assertEqual(len(related), 3)


if __name__ == "__main__":
    unittest.main()
