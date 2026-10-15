"""Static guard, stricter than the one in test_scoped_session.py: new code may not open sessions outside the audited modules.
Written, not run (test pause)."""

import ast
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("_scoped_session_tests", Path(__file__).with_name("test_scoped_session.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
RAW_SESSION_ALLOWLIST = _mod.RAW_SESSION_ALLOWLIST

ROOT = Path(__file__).resolve().parent.parent / "app"
# `db.sessions()` is the factory ScopedDatabase wraps; only app/db may call it directly.
SESSIONS_FACTORY_OK = {"db/scoped.py", "db/engine.py"}


def _calls(path: Path, attr: str):
    return [n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == attr]


def test_nothing_outside_app_db_calls_the_raw_session_factory():
    bad = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
                 if str(p.relative_to(ROOT)) not in SESSIONS_FACTORY_OK and _calls(p, "sessions"))
    assert not bad, f"use ScopedDatabase(...).session(): {bad}"


def test_http_routers_never_use_a_raw_session():
    """Routers speak for a signed-in user, so they must go through a scoped session or a service."""
    routers = [p for p in ROOT.rglob("*.py") if p.name.endswith("router.py")]
    assert routers
    offenders = [str(p.relative_to(ROOT)) for p in routers if str(p.relative_to(ROOT)) in RAW_SESSION_ALLOWLIST]
    assert offenders == []


def test_tasks_router_reads_go_through_a_scoped_session():
    src = (ROOT / "tasks_router.py").read_text()
    assert "ScopedDatabase" in src and ".db.session()" not in src


def test_org_owned_models_include_channel_members():
    from app.db.scoped import org_owned_models
    assert "channel_members" in {m.__tablename__ for m in org_owned_models()}
