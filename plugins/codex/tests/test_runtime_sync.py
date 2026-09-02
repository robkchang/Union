from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "plugins" / "codex" / "runtime"
PACKAGES = ((ROOT / "node" / "union_node", RUNTIME / "union_node"),
            (ROOT / "protocol" / "union_protocol", RUNTIME / "union_protocol"))


def test_runtime_matches_shared_packages():
    for source, bundled in PACKAGES:
        source_files = {path.relative_to(source) for path in source.rglob("*.py")}
        bundled_files = {path.relative_to(bundled) for path in bundled.rglob("*.py")}
        assert bundled_files == source_files
        for relative in source_files:
            assert (bundled / relative).read_bytes() == (source / relative).read_bytes()
