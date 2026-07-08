"""Hermetic tests for the deterministic parts of the backfill script
(discovery + source reading). The LLM generation itself runs against the live
proxy, not here."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_backfill_module():
    path = Path(__file__).parents[2] / "scripts" / "backfill_plugin_specs.py"
    spec = importlib.util.spec_from_file_location("backfill_plugin_specs", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_fwbg_tree(root: Path) -> Path:
    bundle = root / "src" / "fwbg" / "plugins" / "fwbg-core"
    (bundle).mkdir(parents=True)
    # Manifest declares only "foo"; "bar" exists on disk but is undeclared.
    (bundle / "manifest.json").write_text(
        json.dumps({"version": "1.0.0", "plugins": {"indicators": ["foo"]}})
    )
    foo = bundle / "indicators" / "foo"
    foo.mkdir(parents=True)
    (foo / "__init__.py").write_text("class FooIndicator:  # SRCMARKER\n    pass\n")
    (foo / "tests.py").write_text("def test_x():  # TESTMARKER\n    pass\n")
    bar = bundle / "indicators" / "bar"
    bar.mkdir(parents=True)
    (bar / "__init__.py").write_text("class BarIndicator:\n    pass\n")
    return foo


def test_iter_plugins_finds_declared_and_undeclared(tmp_path):
    mod = _load_backfill_module()
    foo_dir = _fake_fwbg_tree(tmp_path)
    entries = list(mod._iter_plugins(tmp_path))
    slugs = {slug for slug, _cat, _dir in entries}
    # both the manifest-declared "foo" and the undeclared "bar" are covered
    assert {"foo", "bar"} <= slugs
    assert ("foo", "indicators", foo_dir) in entries


def test_read_plugin_source_excludes_tests(tmp_path):
    mod = _load_backfill_module()
    foo_dir = _fake_fwbg_tree(tmp_path)
    src = mod._read_plugin_source(foo_dir)
    assert "SRCMARKER" in src
    assert "TESTMARKER" not in src
