from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, TextIO

from .fileio import (
    ensure_directory,
    fsync_directory,
    open_existing_regular,
    open_or_create_regular,
)
from .sharing import share_example
from .userdirs import xdg_user_directory


CLI_TIP = "cli-tip"
TUI_TOUR = "tui-tour"
ONBOARDING_KINDS = frozenset({CLI_TIP, TUI_TOUR})
ONBOARDING_VERSION = 1
ONBOARDING_FILE_MODE = 0o600
ONBOARDING_DIRECTORY_MODE = 0o700
MAX_ONBOARDING_BYTES = 128


@dataclass(frozen=True)
class TutorialPage:
    title: str
    summary: str
    commands: tuple[tuple[str, str], ...]


def tutorial_pages(config) -> tuple[TutorialPage, ...]:
    slice_text = f"{config.slot_minutes}m"
    gpu_count = max(1, int(config.gpu_count))
    multi_gpu_count = min(2, gpu_count)
    example_gpu = min(3, gpu_count - 1)
    example_share = share_example(max(1, int(config.max_shared_users)))
    return (
        TutorialPage(
            "Book a GPU",
            f"Shared is the default. This server books in {slice_text} intervals.",
            (
                ("bk 1 30m", "book one GPU at the earliest suitable time"),
                (
                    f"bk {multi_gpu_count} 1h30m --mem 12g",
                    "declare expected VRAM separately for every GPU",
                ),
                ("bk x 1 1h", "book one GPU exclusively"),
                (
                    f"bk slots {multi_gpu_count} 1h",
                    "preview alternatives without creating anything",
                ),
            ),
        ),
        TutorialPage(
            "Choose time and sharing",
            "Omit a start time to queue automatically; an explicit time never moves.",
            (
                ("bk 1 1h --at +30m", "start exactly 30 minutes from now"),
                ("bk 1 1h --at 20:00", "use local wall-clock time"),
                (
                    f"bk 1 1h --share {example_share}",
                    "request more integer shared slots",
                ),
                (f"bk 1 1h --gpu {example_gpu}", "request one specific GPU"),
            ),
        ),
        TutorialPage(
            "See and manage",
            "List numbers and unique short IDs both work for your reservations.",
            (
                ("bk info", "show the responsible administrator and contact"),
                ("bk st", "show compact live status"),
                ("bk tl 8h --step 15m", "show a detailed eight-hour timeline"),
                ("bk l", "list your active reservations"),
                ("bk e 1", "edit with recoverable prompts"),
                ("bk d 1", "cancel your reservation"),
            ),
        ),
        TutorialPage(
            "Use the TUI",
            "The full-screen view adds a zoomable timeline and keyboard selection.",
            (
                ("bk t", "open the TUI; its first launch shows a guided tour"),
                ("a", "add from the timeline"),
                ("1-9", "choose a GPU count and find the nearest valid slot"),
                ("Tab / arrows", "switch focus and move through rows or time"),
                ("?", "reopen the paged TUI help"),
            ),
        ),
        TutorialPage(
            "Run and inspect work",
            "A command after -- can run at reservation time on assigned devices.",
            (
                ("bk 1 1h -- python train.py", "book and schedule a command"),
                ("bk w --status", "check your private scheduled-command worker"),
                ("bk j", "list your scheduled jobs"),
                ("bk u", "summarize your recent GPU use"),
                ("bk doctor", "run read-only deployment and ledger checks"),
            ),
        ),
    )


def run_cli_tutorial(
    config,
    *,
    input_stream: Optional[TextIO] = None,
    output: Optional[TextIO] = None,
    environment: Optional[Mapping[str, str]] = None,
    interactive: Optional[bool] = None,
    color: Optional[bool] = None,
) -> str:
    input_stream = input_stream or sys.stdin
    output = output or sys.stdout
    environment = os.environ if environment is None else environment
    pages = tutorial_pages(config)
    if interactive is None:
        interactive = _isatty(input_stream) and _isatty(output)
    if color is None:
        color = interactive and _color_enabled(output, environment)

    if not interactive:
        for index, page in enumerate(pages):
            _print_page(page, index, len(pages), output, color=color)
        print("Try the visual tour with: bk tutorial --tui", file=output)
        return "done"

    page_index = 0
    while True:
        _print_page(pages[page_index], page_index, len(pages), output, color=color)
        prompt = "[Enter] next  [b] back  [t] TUI tour  [q] quit > "
        print(_style(prompt, "prompt", color), end="", file=output, flush=True)
        answer = input_stream.readline()
        if answer == "":
            return "done"
        choice = answer.strip().lower()
        if choice in {"q", "quit", "exit"}:
            return "done"
        if choice in {"t", "tui"}:
            return "tui"
        if choice in {"b", "back", "left"}:
            page_index = max(0, page_index - 1)
            continue
        if choice in {"", "n", "next", "right"}:
            if page_index == len(pages) - 1:
                print("Tutorial complete. Reopen it anytime with: bk tutorial", file=output)
                return "done"
            page_index += 1
            continue
        print("Choose Enter, b, t, or q.", file=output)


def onboarding_marker_path(
    kind: str,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> Path:
    _validate_onboarding_kind(kind)
    state_home = xdg_user_directory(
        "XDG_STATE_HOME",
        ".local/state",
        environment=environment,
    )
    return state_home / "bk" / f"onboarding-v{ONBOARDING_VERSION}-{kind}.seen"


def onboarding_seen(
    kind: str,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    path = onboarding_marker_path(kind, environment=environment)
    try:
        fd = open_existing_regular(path, expected_mode=ONBOARDING_FILE_MODE)
    except (FileNotFoundError, OSError):
        return False
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != os.getuid():
            return False
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read(MAX_ONBOARDING_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    return payload == _marker_payload(kind)


def mark_onboarding_seen(
    kind: str,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> Path:
    path = onboarding_marker_path(kind, environment=environment)
    ensure_directory(path.parent, ONBOARDING_DIRECTORY_MODE, require_mode=True)
    fd = open_or_create_regular(
        path,
        os.O_WRONLY,
        ONBOARDING_FILE_MODE,
    )
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise PermissionError(f"onboarding marker is not owned by UID {os.getuid()}: {path}")
        payload = _marker_payload(kind)
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    fsync_directory(path.parent)
    return path


def _print_page(
    page: TutorialPage,
    index: int,
    total: int,
    output: TextIO,
    *,
    color: bool,
) -> None:
    print(file=output)
    heading = f"GPUBK tutorial {index + 1}/{total}  {page.title}"
    print(_style(heading, "heading", color), file=output)
    print(_style(page.summary, "muted", color), file=output)
    print(file=output)
    width = max(len(command) for command, _description in page.commands)
    for command, description in page.commands:
        label = _style(command.ljust(width), "command", color)
        print(f"  {label}  {description}", file=output)
    print(file=output)


def _color_enabled(output: TextIO, environment: Mapping[str, str]) -> bool:
    return (
        _isatty(output)
        and "NO_COLOR" not in environment
        and environment.get("TERM", "") != "dumb"
    )


def _style(text: str, role: str, enabled: bool) -> str:
    if not enabled:
        return text
    code = {
        "heading": "1;36",
        "muted": "2",
        "command": "1;32",
        "prompt": "1;34",
    }[role]
    return f"\x1b[{code}m{text}\x1b[0m"


def _marker_payload(kind: str) -> bytes:
    _validate_onboarding_kind(kind)
    return f"gpubk-onboarding-v{ONBOARDING_VERSION}:{kind}\n".encode("ascii")


def _validate_onboarding_kind(kind: str) -> None:
    if kind not in ONBOARDING_KINDS:
        raise ValueError(f"unknown onboarding marker: {kind!r}")


def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False
