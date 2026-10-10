"""Top-level PiProbe command-line interface."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from piprobe import __version__, announce, discover, openocd, program


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="piprobe",
        description="Discover and use network-attached PiProbe nodes.",
    )
    parser.add_argument("--version", action="version", version=f"piprobe {__version__}")
    if not arguments or arguments[0] in ("-h", "--help"):
        parser.print_help()
        return 0
    if arguments[0] == "discover":
        return discover.main(arguments[1:])
    if arguments[0] == "announce":
        return announce.main(arguments[1:])
    if arguments[0] == "openocd":
        return openocd.main(arguments[1:])
    if arguments[0] == "program":
        return program.main(arguments[1:])
    if arguments[0] == "--version":
        print(f"piprobe {__version__}")
        return 0
    parser.error(f"unknown command: {arguments[0]}")


if __name__ == "__main__":
    raise SystemExit(main())
