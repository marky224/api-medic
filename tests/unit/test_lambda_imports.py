"""Static-import audit for the Lambda surface.

The hosted-demo Lambda exposes /api/run (live runner) alongside
/api/analyze (captured-mode), so httpx / dnspython / cryptography
ARE expected — they're the runner's deps. What must still NEVER
appear in the Lambda surface: fastapi, uvicorn (the Lambda dispatches
routes inline; no web framework), and the CLI/terminal-render deps
(typer, click, rich) which would just bloat the zip.

Rather than try to test this dynamically (which is unreliable in a
session that's already imported the heavy deps for other tests), we
parse handler.py and the modules it transitively reaches via api_medic
imports, and assert no module name in the forbidden set appears.

If a new dependency is introduced into the Lambda surface accidentally,
this test fails with a clear message pointing at the file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.parent
HANDLER = ROOT / "deploy" / "lambda" / "handler.py"
SRC = ROOT / "src"

# Modules that must never appear in the Lambda surface's transitive imports.
FORBIDDEN = {
    "fastapi",
    "uvicorn",
    "rich",
    "typer",  # CLI surface
    "click",  # CLI surface (typer's transitive)
}


def _file_imports(path: Path) -> set[str]:
    """Top-level module names imported by `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[0])
    return out


def _file_qualified_imports(path: Path) -> set[str]:
    """Fully-qualified module names imported by `path` (for api_medic.* tracing)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()

    # Compute the file's own package path so relative imports resolve.
    # handler.py lives outside src/ — relative imports aren't valid there
    # anyway, so an empty pkg is fine for it.
    pkg: list[str] = []
    try:
        rel = path.resolve().relative_to(SRC.resolve())
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        pkg = parts
    except ValueError:
        pkg = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                if not pkg:
                    continue  # relative import in a non-package file — skip
                base = pkg[: len(pkg) - (node.level - 1)]
                if node.module:
                    base = [*base, *node.module.split(".")]
                out.add(".".join(base))
            elif node.module:
                out.add(node.module)
    return out


def _module_to_path(qualified: str) -> Path | None:
    """Resolve an api_medic.* qualified name to a file path under src/."""
    if not qualified.startswith("api_medic"):
        return None
    parts = qualified.split(".")
    candidate = SRC.joinpath(*parts).with_suffix(".py")
    if candidate.exists():
        return candidate
    init = SRC.joinpath(*parts) / "__init__.py"
    if init.exists():
        return init
    return None


def _walk_lambda_surface() -> set[Path]:
    """BFS from handler.py through api_medic.* imports. Returns every file
    that ends up in the Lambda's import graph."""
    visited: set[Path] = set()
    queue: list[Path] = [HANDLER]
    while queue:
        path = queue.pop()
        if path in visited:
            continue
        visited.add(path)
        for qualified in _file_qualified_imports(path):
            sub = _module_to_path(qualified)
            if sub is not None and sub not in visited:
                queue.append(sub)
    return visited


class TestLambdaImportsAreLean:
    def test_handler_top_level_imports(self):
        bad = _file_imports(HANDLER) & FORBIDDEN
        assert not bad, f"handler.py top-level imports forbidden modules: {bad}"

    def test_handler_reaches_runner(self):
        """Live-run path requires the runner; verify it's actually in the graph."""
        graph = _walk_lambda_surface()
        runner = SRC / "api_medic" / "core" / "runner.py"
        assert runner in graph, (
            "Lambda surface no longer reaches core/runner.py — /api/run will "
            "be broken. handler.py should import api_medic.core.runner."
        )

    def test_handler_reaches_runner_safety(self):
        """SSRF guard must be in the live-run path."""
        graph = _walk_lambda_surface()
        safety = SRC / "api_medic" / "core" / "runner_safety.py"
        assert safety in graph, (
            "Lambda surface no longer reaches core/runner_safety.py — /api/run "
            "would be open SSRF. handler.py must call check_url_safe before run_request."
        )

    def test_handler_does_not_reach_web_app(self):
        graph = _walk_lambda_surface()
        web = SRC / "api_medic" / "web" / "app.py"
        assert web not in graph, (
            "Lambda surface reaches web/app.py — that drags in fastapi/uvicorn."
        )

    def test_handler_does_not_reach_cli(self):
        graph = _walk_lambda_surface()
        cli = SRC / "api_medic" / "cli" / "main.py"
        assert cli not in graph, "Lambda surface reaches cli/main.py — that drags in typer."

    def test_handler_does_not_reach_render_terminal(self):
        graph = _walk_lambda_surface()
        terminal = SRC / "api_medic" / "core" / "render" / "terminal.py"
        assert terminal not in graph, (
            "Lambda surface reaches render/terminal.py — that drags in rich. "
            "Use Report.model_dump_json() directly, not the render package."
        )

    @pytest.mark.parametrize("forbidden", sorted(FORBIDDEN))
    def test_no_module_in_lambda_surface_imports_forbidden(self, forbidden):
        graph = _walk_lambda_surface()
        offenders: list[str] = []
        for path in graph:
            top_imports = _file_imports(path)
            if forbidden in top_imports:
                offenders.append(str(path.relative_to(ROOT)))
        assert not offenders, (
            f"Files in the Lambda surface import forbidden module {forbidden!r}: {offenders}"
        )
