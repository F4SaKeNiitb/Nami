"""Generated-file TTL purge stays inside its folder."""

import os
import time

from app.core.cleanup import purge_generated


def _age(path, hours):
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


def test_old_files_go_new_files_stay_and_empty_folders_are_removed(tmp_path):
    root = tmp_path / "generated"
    (root / "u1").mkdir(parents=True)
    (root / "u2").mkdir()
    old, new, only_old = root / "u1" / "a.png", root / "u1" / "b.png", root / "u2" / "c.png"
    for f in (old, new, only_old):
        f.write_bytes(b"x")
    _age(old, 100)
    _age(only_old, 100)
    assert purge_generated(root, 72) == 2
    assert not old.exists() and new.exists() and not only_old.exists() and not (root / "u2").exists()


def test_zero_ttl_keeps_everything(tmp_path):
    (tmp_path / "u").mkdir()
    f = tmp_path / "u" / "a.png"
    f.write_bytes(b"x")
    _age(f, 1000)
    assert purge_generated(tmp_path, 0) == 0 and f.exists()


def test_symlinks_and_outside_files_are_never_touched(tmp_path):
    root, outside = tmp_path / "generated", tmp_path / "outside"
    (root / "u").mkdir(parents=True)
    outside.mkdir()
    victim = outside / "secret.txt"
    victim.write_text("keep")
    _age(victim, 1000)
    (root / "u" / "link").symlink_to(victim)
    (root / "linkdir").symlink_to(outside, target_is_directory=True)
    stray = root / "top.png"                                 # a file directly in root is not a user folder entry
    stray.write_bytes(b"x")
    _age(stray, 1000)
    assert purge_generated(root, 1) == 0
    assert victim.exists() and stray.exists() and (root / "u" / "link").is_symlink()


def test_missing_folder_is_fine(tmp_path):
    assert purge_generated(tmp_path / "nope", 1) == 0
