"""What goes into the container image, and that the app can write where it must. Written, not run (test pause).

Found at deploy time: `.files` (local chat attachments and result screenshots) was not in .dockerignore, so 88 of the developer's own files were
baked into the image, root-owned, and Chainlit could not write new ones (PermissionError) for the unprivileged app user."""

import fnmatch
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IGNORE = [ln.strip() for ln in (ROOT / ".dockerignore").read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def ignored(rel: str) -> bool:
    parts = rel.split("/")
    for pat in IGNORE:
        pat = pat.rstrip("/")
        if any(fnmatch.fnmatch(p, pat) for p in parts) or fnmatch.fnmatch(rel, pat) or rel.startswith(pat + "/"):
            return True
    return False


def test_local_state_and_secrets_are_kept_out_of_the_image():
    for must in (".env", "data", ".files", ".venv", ".git", "tests"):
        assert must in IGNORE, f"{must} must be in .dockerignore"


def test_nothing_that_looks_like_a_secret_or_a_database_would_be_copied_into_the_image():
    forbidden = (".db", ".sqlite", ".pem", ".key", ".p12", ".pfx")
    leaked = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT).replace(os.sep, "/")
        join = lambda a, b: b if a == "." else f"{a}/{b}"                       # (lstrip("./") once turned ".venv" into "venv" and matched nothing)
        dirnames[:] = [d for d in dirnames if not ignored(join(rel_dir, d))]
        for f in filenames:
            rel = join(rel_dir, f)
            if ignored(rel):
                continue
            if f.endswith(forbidden) or f == ".env":
                leaked.append(rel)
    assert leaked == [], f"would be baked into the image: {leaked}"


def test_the_attachment_folder_is_inside_the_data_volume_and_writable_by_the_app_user():
    docker = (ROOT / "Dockerfile").read_text()
    assert "ln -s /srv/data/files /srv/.files" in docker
    assert docker.index("ln -s /srv/data/files /srv/.files") < docker.index("USER nami")         # created as root, before dropping privileges
    assert "chown -R nami /srv/data" in docker
    assert "mkdir -p /srv/data/files" in (ROOT / "infra" / "entrypoint.sh").read_text()          # a fresh, empty volume still works


def test_the_deploy_script_removes_old_image_layers_so_a_bad_image_does_not_linger_on_the_vm():
    # after the restart, not before: the old image is still in use by the running container until then
    start = (ROOT / "infra" / "azure-vm" / "remote-start.sh").read_text()
    assert start.index("systemctl restart nami") < start.index("docker image prune -f")


def test_allow_email_script_rejects_anything_that_could_act_as_shell_or_sed_syntax():
    src = (ROOT / "infra" / "azure-vm" / "allow-email.sh").read_text()
    assert "*[!a-z0-9._%+@-]*" in src                                   # only plain address characters get through
    assert src.index("exit 1") < src.index("vm_run")                    # validated before anything is sent to the VM
    assert "already allowed" in src                                     # idempotent


def test_container_logs_are_capped_so_a_chatty_agent_cannot_fill_the_small_disk():
    import yaml
    d = yaml.safe_load((ROOT / "infra" / "azure-vm" / "docker-compose.yml").read_text())
    for name, svc in d["services"].items():
        opts = svc["logging"]["options"]
        assert opts["max-size"] == "10m" and opts["max-file"] == "3", name


def test_logs_script_validates_what_it_sends_to_the_vm():
    src = (ROOT / "infra" / "azure-vm" / "logs.sh").read_text()
    assert "*[!A-Za-z0-9\\ _.:-]*" in src and "need a number" in src      # filter text and counts are checked before anything runs remotely
