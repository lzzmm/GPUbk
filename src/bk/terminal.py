from __future__ import annotations

import os
import sys
import unicodedata
from typing import Mapping, Optional, TextIO


ANSI = {
    "heading": "\x1b[1;36m",
    "success": "\x1b[32m",
    "warning": "\x1b[33m",
    "error": "\x1b[1;31m",
    "accent": "\x1b[36m",
    "id": "\x1b[35m",
    "muted": "\x1b[2m",
}
RESET = "\x1b[0m"


def color_enabled(
    stream: Optional[TextIO] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    output = stream or sys.stdout
    env = os.environ if environment is None else environment
    try:
        terminal = bool(output.isatty())
    except (AttributeError, OSError):
        terminal = False
    return terminal and "NO_COLOR" not in env and env.get("TERM", "") != "dumb"


def style(text: str, role: str, *, enabled: bool) -> str:
    code = ANSI.get(role)
    if not enabled or code is None:
        return text
    return f"{code}{text}{RESET}"


def display_width(text: str) -> int:
    return sum(_character_width(char) for char in text)


def pad_display_text(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def clip_display_text(text: str, width: int, *, suffix: str = "...") -> str:
    if display_width(text) <= width:
        return text
    suffix_width = display_width(suffix)
    if suffix_width >= width:
        suffix = ""
        suffix_width = 0
    retained = []
    retained_width = 0
    limit = width - suffix_width
    for char in text:
        char_width = _character_width(char)
        if retained_width + char_width > limit:
            break
        retained.append(char)
        retained_width += char_width
    return "".join(retained).rstrip() + suffix


def wrap_display_text(
    text: str,
    width: int,
    *,
    subsequent_indent: str = "  ",
    preserve_newlines: bool = True,
) -> list[str]:
    """Wrap text by terminal cells, accounting for wide CJK characters."""
    if width < 1:
        return [""]
    source = str(text)
    paragraphs = source.split("\n") if preserve_newlines else [" ".join(source.split())]
    output: list[str] = []
    for paragraph in paragraphs:
        current = ""
        current_width = 0
        for char in paragraph:
            char_width = _character_width(char)
            if current and current_width + char_width > width:
                break_at = max(current.rfind(" "), current.rfind("\t"))
                if break_at >= len(subsequent_indent):
                    output.append(current[:break_at].rstrip())
                    remainder = current[break_at + 1 :].lstrip()
                    current = subsequent_indent + remainder
                    current_width = display_width(current)
                else:
                    output.append(current.rstrip())
                    current = subsequent_indent
                    current_width = display_width(subsequent_indent)
            current += char
            current_width += char_width
        output.append(current.rstrip())
    return output


def _character_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def colorize_help(text: str, *, enabled: bool) -> str:
    if not enabled:
        return text
    section_names = {
        "START HERE",
        "BOOK",
        "VIEW",
        "MANAGE",
        "JOBS AND USAGE",
        "AUTOMATION",
        "ADMINISTRATION (current administrator only)",
        "TIME AND POLICY",
    }
    lines = []
    for line in text.splitlines():
        if line in section_names:
            line = style(line, "heading", enabled=True)
        elif line.startswith("GPUBK -"):
            line = style(line, "accent", enabled=True)
        lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
