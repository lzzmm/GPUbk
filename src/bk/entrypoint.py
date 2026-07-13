from __future__ import annotations

import sys
from collections.abc import Sequence

from . import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Start GPUBK without loading the full command tree for version queries."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"-V", "--version", "version"}:
        print(f"bk {__version__}")
        return 0

    from .cli import main as cli_main

    return cli_main(arguments)
