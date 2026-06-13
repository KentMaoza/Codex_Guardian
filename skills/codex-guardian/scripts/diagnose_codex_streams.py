#!/usr/bin/env python3
"""Lightweight Codex stream diagnostic entrypoint."""

from pathlib import Path
import runpy
import sys


def main() -> None:
    guardian = Path(__file__).with_name("codex_guardian.py")
    sys.argv = [str(guardian), "diagnose", *sys.argv[1:]]
    runpy.run_path(str(guardian), run_name="__main__")


if __name__ == "__main__":
    main()
