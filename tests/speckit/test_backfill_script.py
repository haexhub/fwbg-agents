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
    """Core bundle with a manifest declaring only `foo`, plus an UNdeclared
    plugin `bar` on disk. `_iter_plugins` walks the disk, so it must find both
    regardless of the manifest."""
    bundle = root / "src" / "fwbg" / "plugins" / "fwbg-core"
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text(
        json.dumps({"version": "1.0.0", "plugins": {"indicators": ["foo"]}})
    )
    foo = bundle / "indicators" / "foo"
    foo.mkdir(parents=True)
    (foo / "__init__.py").write_text("class FooIndicator:  # SRCMARKER\n    pass\n")
    (foo / "tests.py").write_text("def test_x():  # TESTMARKER\n    pass\n")

    # Undeclared plugin (not in manifest) — still on disk.
    bar = bundle / "indicators" / "bar"
    bar.mkdir(parents=True)
    (bar / "__init__.py").write_text("class BarIndicator:\n    pass\n")
    return foo


def _fake_installed_package(root: Path) -> Path:
    """Plugins dir of a separately installed package (outside the fwbg tree)."""
    plugins_dir = root / "site" / "fwbg_premium" / "plugins"
    baz = plugins_dir / "fwbg-premium" / "models" / "baz"
    baz.mkdir(parents=True)
    (baz / "__init__.py").write_text("class BazModel:\n    pass\n")
    return plugins_dir


def test_iter_plugins_finds_declared_and_undeclared(tmp_path):
    mod = _load_backfill_module()
    foo_dir = _fake_fwbg_tree(tmp_path)
    package_dir = _fake_installed_package(tmp_path)
    entries = list(mod._iter_plugins(tmp_path, extra_roots=[package_dir]))
    slugs = {slug for slug, _cat, _dir in entries}
    # manifest-declared, undeclared, and installed-package plugins all discovered
    assert {"foo", "bar", "baz"} <= slugs
    assert ("foo", "indicators", foo_dir) in entries
    assert foo_dir.is_dir()


def test_iter_plugins_core_only_without_installed_package(tmp_path):
    mod = _load_backfill_module()
    _fake_fwbg_tree(tmp_path)
    slugs = {slug for slug, _cat, _dir in mod._iter_plugins(tmp_path, extra_roots=[])}
    assert slugs == {"foo", "bar"}


def test_old_in_tree_premium_location_is_not_scanned(tmp_path):
    mod = _load_backfill_module()
    _fake_fwbg_tree(tmp_path)
    old = tmp_path / "packages" / "fwbg-premium" / "src" / "fwbg_premium" / "plugins"
    stale = old / "fwbg-premium" / "models" / "stale"
    stale.mkdir(parents=True)
    (stale / "__init__.py").write_text("class Stale:\n    pass\n")
    slugs = {slug for slug, _cat, _dir in mod._iter_plugins(tmp_path, extra_roots=[])}
    assert "stale" not in slugs


class _FakeEntryPoint:
    def __init__(self, name, loaded):
        self.name = name
        self._loaded = loaded

    def load(self):
        if isinstance(self._loaded, Exception):
            raise self._loaded
        return self._loaded


def test_installed_plugin_roots_resolves_entry_points(tmp_path, monkeypatch):
    mod = _load_backfill_module()
    package_dir = _fake_installed_package(tmp_path)
    eps = [
        _FakeEntryPoint("fwbg-premium", lambda: package_dir),
        _FakeEntryPoint("dup", lambda: package_dir),
        _FakeEntryPoint("broken", ImportError("boom")),
        _FakeEntryPoint("missing", lambda: tmp_path / "nope"),
    ]
    monkeypatch.setattr(mod, "entry_points", lambda group: eps)
    assert mod.installed_plugin_roots() == [package_dir.resolve()]


def test_installed_plugin_roots_empty_when_not_installed(monkeypatch, caplog):
    mod = _load_backfill_module()
    monkeypatch.setattr(mod, "entry_points", lambda group: [])
    with caplog.at_level("INFO", logger="backfill_plugin_specs"):
        assert mod.installed_plugin_roots() == []
    assert "core plugins only" in caplog.text


def test_read_plugin_source_excludes_tests(tmp_path):
    mod = _load_backfill_module()
    foo_dir = _fake_fwbg_tree(tmp_path)
    src = mod._read_plugin_source(foo_dir)
    assert "SRCMARKER" in src
    assert "TESTMARKER" not in src
