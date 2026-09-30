"""No function may read a name BEFORE re-importing it inside its own body (23 Sep 2026).

    from models import Opportunity            # module level
    def list_requirements(...):
        stmt = stmt.join(Opportunity, ...)    # ← UnboundLocalError at runtime
        ...
        from models import Opportunity        # makes the name a LOCAL for the whole function

That is a runtime error Python raises only when the early line executes, so it
ships green and 500s in production — it took every TA stage tab down on
23 Sep 2026, and the same shape sat in `resumes.upload_resume`'s duplicate
branch. pyflakes does not report it. This walks the AST of every production
module and fails on the pattern.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_PARTS = {"tests", "_archive", "_to_delete", "newfiles", "alembic", "scripts", "tools", "__pycache__"}


def _findings() -> list[str]:
    hits: list[str] = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except SyntaxError as exc:  # a syntax error is its own failure
            hits.append(f"{path}: {exc}")
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            inner: dict[str, int] = {}
            loads: list[tuple[str, int]] = []

            def walk(node):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                        continue  # nested scopes have their own locals
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        for alias in child.names:
                            inner.setdefault((alias.asname or alias.name).split(".")[0], child.lineno)
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                        loads.append((child.id, child.lineno))
                    walk(child)

            walk(fn)
            params = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
            for name, imp_line in inner.items():
                if name in params:
                    continue
                early = [ln for n, ln in loads if n == name and ln < imp_line]
                if early:
                    rel = path.relative_to(ROOT)
                    hits.append(f"{rel}:{early[0]} `{name}` is read before its inner import on line {imp_line} (in {fn.name})")
    return sorted(hits)


def test_no_name_is_read_before_its_inner_import():
    found = _findings()
    assert not found, "Shadowed import → UnboundLocalError at runtime:\n" + "\n".join(found)
