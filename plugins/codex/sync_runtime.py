"""Synchronize the self-contained Codex runtime with Union's shared packages."""
from __future__ import annotations

import pathlib
import shutil


ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNTIME = pathlib.Path(__file__).resolve().parent / "runtime"
PACKAGES = ("union_node", "union_protocol")


def main() -> None:
    for package in PACKAGES:
        source = ROOT / ("node" if package == "union_node" else "protocol") / package
        destination = RUNTIME / package
        shutil.copytree(source, destination, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


if __name__ == "__main__":
    main()
