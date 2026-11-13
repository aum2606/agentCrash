"""Forbidden-import check (IMPLEMENTATION.md section 1): inner layers never import outward."""

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "src" / "agentcrash"

# package -> internal packages it may import (itself is always allowed). Anything not listed
# (cli, bootstrap, settings, codegen) is a composition layer and may import everything.
ALLOWED: dict[str, set[str]] = {
    "domain": set(),
    "scenarios": {"domain"},
    "environments": {"domain"},
    "agents": {"domain"},
    "storage": {"domain"},
    "faults": {"domain"},
    "replay": {"domain"},
    "experiments": {"domain"},
    # The API sees persistence only through domain ports; bootstrap injects the implementation.
    "api": {"domain", "scenarios"},
}


def is_internal(name: str) -> bool:
    return (PKG / name).is_dir() or (PKG / f"{name}.py").is_file()


def internal_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: resolve against the module's package
                pytest.fail(f"{path}: use absolute imports so layering can be checked")
            names = [node.module or ""]
            if node.module == "agentcrash":
                names = [f"agentcrash.{a.name}" for a in node.names]
        for name in names:
            parts = name.split(".")
            # Count only real sub-packages/modules (not names like ``agentcrash.__version__``).
            if parts[0] == "agentcrash" and len(parts) > 1 and is_internal(parts[1]):
                found.add(parts[1])
    return found


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_layer_imports_only_allowed_packages(layer: str) -> None:
    files = sorted((PKG / layer).rglob("*.py"))
    assert files, f"layer {layer} has no modules"
    for path in files:
        illegal = internal_imports(path) - ALLOWED[layer] - {layer}
        assert not illegal, f"{path.relative_to(PKG)} imports forbidden layer(s): {sorted(illegal)}"


def test_checker_detects_a_violation(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("from agentcrash.api.app import create_app\nimport agentcrash.cli\n")
    assert internal_imports(bad) == {"api", "cli"}


def test_the_intentionally_vulnerable_parser_is_imported_only_by_the_naive_agent() -> None:
    allowed = {PKG / "agents" / "naive.py", PKG / "agents" / "insecure_demo.py"}
    for path in PKG.rglob("*.py"):
        if path in allowed:
            continue
        assert "insecure_demo" not in path.read_text(encoding="utf-8"), path.relative_to(PKG)
