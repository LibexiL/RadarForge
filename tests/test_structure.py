"""Project structure: every import resolves, including the ones inside functions that pyflakes can't see."""
from __future__ import annotations

import ast
import importlib
import os
import pkgutil
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import radarforge  # noqa: E402

ROOT = Path(radarforge.__file__).parent


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def test_every_module_imports():
    for info in pkgutil.walk_packages(radarforge.__path__, "radarforge."):
        if info.name.endswith("__main__"):
            continue
        importlib.import_module(info.name)


def test_every_relative_import_resolves():
    """Imports nested in functions only fail when they run; resolve all of them statically."""
    problems = []
    for path in ROOT.rglob("*.py"):
        mod = _module_name(path)
        pkg = mod if path.name == "__init__.py" else mod.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            base = pkg.split(".")
            if node.level - 1 > len(base) - 1:
                problems.append(f"{mod}:{node.lineno} goes above the top package")
                continue
            base = base[:len(base) - (node.level - 1)]
            target = ".".join(base + ([node.module] if node.module else []))
            try:
                m = importlib.import_module(target)
            except Exception as exc:
                problems.append(f"{mod}:{node.lineno} cannot import {target}: {exc}")
                continue
            for alias in node.names:
                if alias.name == "*" or hasattr(m, alias.name):
                    continue
                try:
                    importlib.import_module(f"{target}.{alias.name}")
                except Exception:
                    problems.append(f"{mod}:{node.lineno} {target} has no {alias.name}")
    assert not problems, "\n".join(problems)


def test_packages_have_a_home_for_everything():
    """The top-level layout stays tidy: only the known packages, no stray modules."""
    packages = {p.name for p in ROOT.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    assert packages >= {"data", "products", "render", "overlays", "services", "tools", "ui"}
    assert "features" not in packages
    modules = {p.stem for p in ROOT.glob("*.py")}
    assert modules <= {"__init__", "__main__", "app", "config", "fmt", "gl_setup", "themes"}, modules
