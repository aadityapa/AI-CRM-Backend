"""One unimportable CRM module must not take down the whole CRM API.

This has now happened twice. Both times the shape was identical: a single bad
import inside the one big `from routers.crm import a, b, c, ...` statement
aborted the entire statement, main.py caught and logged it, and the app started
and passed health checks with all ~250 CRM endpoints returning 404. Both times
it was first reported as a frontend bug.
"""
from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import routers.crm as crm  # noqa: E402


@pytest.fixture(autouse=True)
def _quiet():
    """The registry logs an exception traceback per failure by design."""
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def restore_import():
    real = crm.importlib.import_module
    yield real
    crm.importlib.import_module = real


def _paths(app: FastAPI) -> set[str]:
    return set(app.openapi()["paths"])


def test_every_module_registers_normally():
    app = FastAPI()
    crm.register_crm_routers(app)
    paths = _paths(app)
    assert len(paths) > 200, f"expected the full CRM surface, got {len(paths)}"
    assert any(p.startswith("/api/candidate-profiles") for p in paths)


def test_a_broken_module_costs_only_its_own_routes(restore_import):
    """The point of the change: failure is contained."""
    healthy = _paths_with_module_broken(restore_import, None)
    damaged = _paths_with_module_broken(restore_import, ".finance")

    assert damaged, "a single bad module must not empty the whole API"
    assert len(damaged) < len(healthy), "the broken module's routes should be gone"
    # Everything else is untouched.
    assert any(p.startswith("/api/candidate-profiles") for p in damaged)
    assert any(p.startswith("/api/opportunities") for p in damaged)
    assert len(healthy) - len(damaged) < 60, "collateral damage is too wide"


def _paths_with_module_broken(real, suffix: str | None) -> set[str]:
    def patched(name, *args, **kwargs):
        if suffix and name.endswith(suffix):
            raise ImportError(f"simulated failure in {name}")
        return real(name, *args, **kwargs)

    crm.importlib.import_module = patched
    app = FastAPI()
    crm.register_crm_routers(app)
    crm.importlib.import_module = real
    return _paths(app)


def test_registration_never_raises(restore_import):
    """main.py wraps this in a try/except; it must not need to."""
    def always_fails(name, *args, **kwargs):
        raise ImportError("everything is broken")

    crm.importlib.import_module = always_fails
    app = FastAPI()
    crm.register_crm_routers(app)  # must not raise
    assert _paths(app) == set()


def test_the_module_list_matches_what_is_on_disk():
    """A file added to routers/crm without being listed registers nothing and
    fails silently — there is no error, the endpoints simply never exist."""
    import pkgutil

    on_disk = {
        m.name for m in pkgutil.iter_modules(crm.__path__)
        if not m.name.startswith("_")
    }
    listed = set(crm._MODULES)
    assert on_disk == listed, (
        f"unlisted on disk: {sorted(on_disk - listed)}; "
        f"listed but missing: {sorted(listed - on_disk)}"
    )


def test_every_listed_module_actually_imports():
    """Catches the 3.11-only-typing class of bug on the interpreter in use."""
    broken = []
    for name in crm._MODULES:
        try:
            importlib.import_module(f"routers.crm.{name}")
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not broken, "modules failed to import: " + "; ".join(broken)
