"""Delete generated files (images, audio) older than a time limit. Only ever touches files inside the generated folder."""

import asyncio
import logging
import time
from pathlib import Path

log = logging.getLogger("nami.cleanup")


def purge_generated(root: Path | str, ttl_hours: float, *, now: float | None = None) -> int:
    """Remove regular files under root/<user>/ whose age exceeds ttl_hours. Symlinks are never followed or removed,
    nothing outside `root` is touched, and emptied user folders are removed. Returns the number of files removed."""
    if ttl_hours <= 0:
        return 0
    base = Path(root)
    if not base.is_dir():
        return 0
    base = base.resolve()
    cutoff = (now if now is not None else time.time()) - ttl_hours * 3600
    removed = 0
    for user_dir in base.iterdir():
        if user_dir.is_symlink() or not user_dir.is_dir():
            continue
        for f in user_dir.iterdir():
            try:
                if f.is_symlink() or not f.is_file() or f.resolve().parent != user_dir.resolve() or f.stat().st_mtime > cutoff:
                    continue
                f.unlink()
                removed += 1
            except OSError:
                log.warning("could not remove a generated file", exc_info=True)
        try:
            user_dir.rmdir()                                  # only succeeds when empty
        except OSError:
            pass
    return removed


async def cleanup_loop(root: Path | str, ttl_hours: float, *, stop: asyncio.Event, interval_s: float = 3600) -> None:
    """At start and then every interval. Errors are logged, never raised."""
    while not stop.is_set():
        try:
            n = await asyncio.to_thread(purge_generated, root, ttl_hours)
            if n:
                log.info("removed %d expired generated file(s)", n)
        except Exception:
            log.warning("generated-file cleanup failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), interval_s)
        except asyncio.TimeoutError:
            pass


async def idle_browser_loop(orchestrator, *, stop: asyncio.Event, interval_s: float = 30) -> None:
    """Every half minute, close the browsers that have been idle too long. Errors are logged, never raised."""
    while not stop.is_set():
        try:
            await orchestrator.reap_idle()
        except Exception:
            log.warning("idle browser sweep failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), interval_s)
        except asyncio.TimeoutError:
            pass
