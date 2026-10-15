"""Supply-chain and production-config checks that run offline: pinned images, locked hashes, declared imports, safe deploy config."""

import ast
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_every_image_reference_is_pinned_by_digest():
    docker = (ROOT / "Dockerfile").read_text()
    refs = re.findall(r"^(?:FROM|COPY --from=)\s*(?:--from=)?(\S+)", docker, re.M)
    refs = [r for r in refs if r and not r.startswith("--") and r != "base"]
    assert refs and all(re.search(r"@sha256:[0-9a-f]{64}$", r) for r in refs), refs
    compose = yaml.safe_load((ROOT / "infra/containerapp.yaml").read_text())
    for c in compose["properties"]["template"]["containers"]:
        if "${" not in c["image"]:
            assert re.search(r"@sha256:[0-9a-f]{64}$", c["image"]), c["image"]


def test_image_runs_as_non_root_and_installs_only_locked_dependencies():
    d = (ROOT / "Dockerfile").read_text()
    assert "uv sync --frozen" in d and re.search(r"^USER\s+(?!root)\S+", d, re.M)
    assert "HEALTHCHECK" in d


def test_lockfile_pins_every_package_with_hashes_and_matches_pyproject():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    missing = []
    for p in lock["package"]:
        if p.get("source", {}).get("virtual") or p.get("source", {}).get("editable"):
            continue
        arts = [*p.get("wheels", []), *([p["sdist"]] if "sdist" in p else [])]
        if not arts or not all(a.get("hash", "").startswith("sha256:") for a in arts):
            missing.append(p["name"])
    assert not missing, missing
    r = subprocess.run(["uv", "lock", "--check"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode == 127 or "not found" in r.stderr:
        pytest.skip("uv not installed")
    assert r.returncode == 0, r.stderr[-500:]


STD = set(__import__("sys").stdlib_module_names)
IMPORT_TO_DIST = {"jwt": "pyjwt", "yaml": "pyyaml", "browser_use": "browser-use", "kernel": "kernel", "opentelemetry": "opentelemetry-sdk",
                  "sqlalchemy": "sqlalchemy", "pydantic_settings": "pydantic-settings", "chainlit": "chainlit",
                  "redis": "redis", "langgraph": "langgraph", "azure": "azure-keyvault-keys", "anthropic": "anthropic"}


def test_every_third_party_import_in_app_is_a_declared_dependency():
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = proj["dependencies"] + [d for extra in proj.get("optional-dependencies", {}).values() for d in extra]
    deps = {re.split(r"[\[<>=! ]", d)[0].lower().replace("_", "-") for d in declared}
    used = set()
    for f in (ROOT / "app").rglob("*.py"):
        if "eval" in f.relative_to(ROOT / "app").parts:        # dev tooling (needs fakeredis); must never be imported by the app
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
            used |= {n.split(".")[0] for n in names}
    third = {u for u in used if u not in STD and u != "app"}
    undeclared = sorted(u for u in third if IMPORT_TO_DIST.get(u, u).lower().replace("_", "-") not in deps)
    assert not undeclared, f"imported but not declared in pyproject: {undeclared}"


def test_deploy_manifest_is_production_safe():
    spec = yaml.safe_load((ROOT / "infra/containerapp.yaml").read_text())["properties"]
    assert spec["template"]["scale"] == {"minReplicas": 1, "maxReplicas": 1}          # SQLite + websockets: exactly one replica
    app = next(c for c in spec["template"]["containers"] if c["name"] == "nami")
    env = {e["name"]: e for e in app["env"]}
    assert env["NAMI_SKILLS_FREEZE"]["value"] == "1" and env["NAMI_KMS_PROVIDER"]["value"] == "azure"
    assert "NAMI_DEV_AUTH" not in env and "NAMI_DEV_PASSWORD" not in env and "NAMI_LOCAL_KEK" not in env
    assert env["NAMI_REDIS_URL"]["value"].startswith("redis://localhost")
    for name in ("NAMI_OPENAI_API_KEY", "NAMI_KERNEL_API_KEY", "OAUTH_GOOGLE_CLIENT_SECRET", "CHAINLIT_AUTH_SECRET"):
        assert "secretRef" in env[name] and "value" not in env[name]                      # secrets only by reference
    assert all(s["keyVaultUrl"].startswith("https://${KV_NAME}") for s in spec["configuration"]["secrets"])
    redis = next(c for c in spec["template"]["containers"] if c["name"] == "redis")
    assert "127.0.0.1" in redis["command"] and "allkeys-lru" in redis["command"]


def test_no_secret_shaped_text_is_committed_in_infra_or_env_example():
    from app.security.secrets import find_secrets
    for f in [*(p for p in (ROOT / "infra").rglob("*") if p.is_file()), ROOT / ".env.example", ROOT / "Dockerfile", *(ROOT / ".github").rglob("*.yml")]:
        hits = [x for x in find_secrets(f.read_text()) if x.kind in ("api_key", "totp_uri")]
        assert not hits, (f.name, hits)


def test_litestream_replicates_every_sqlite_file_the_app_writes():
    cfg = yaml.safe_load((ROOT / "infra/litestream.yml").read_text())
    paths = {d["path"] for d in cfg["dbs"]}
    assert paths == {"/srv/data/nami.db", "/srv/data/chainlit.db", "/srv/data/graph_checkpoints.db"}
    assert (ROOT / "infra/entrypoint.sh").stat().st_mode & 0o111


def test_ci_runs_the_suites_and_the_vulnerability_audit():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    for needle in ("uv lock --check", "pytest", "pip-audit", "--require-hashes", "smoke_graph"):
        assert needle in ci


def test_the_production_app_never_imports_the_eval_tooling():
    code = "import sys, app.main; bad=[m for m in sys.modules if m.startswith('app.eval')]; print(bad); sys.exit(1 if bad else 0)"
    import os, sys
    env = {**os.environ, "PYTHONPATH": str(ROOT), "NAMI_DEV_AUTH": "1", "CHAINLIT_AUTH_SECRET": "x" * 48}
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr[-500:]
