"""Incremental deployment of a local build folder to the console.

Only files whose content changed since the last deploy (sha1 cache) or whose size
differs on the console are uploaded, so the edit -> build -> run loop stays fast.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
from pathlib import Path

from .client import XboxConsole
from .protocol import join_console, normalize_path

DEFAULT_EXCLUDES = ["*.pdb", "*.ilk", "*.obj", "*.lib", "*.exp", "*.iobj", "*.ipdb", ".git", ".vs", "*.tlog", "*.log"]


def _cache_path() -> Path:
    base = Path(os.environ.get("XBOX360_CACHE_DIR") or Path.home() / ".xbox360-mcp")
    base.mkdir(parents=True, exist_ok=True)
    return base / "deploy_cache.json"


def _load_cache() -> dict:
    try:
        return json.loads(_cache_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        _cache_path().write_text(json.dumps(cache, indent=0), "utf-8")
    except OSError:
        pass


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_digest(path: Path, cached: object) -> tuple[str, dict]:
    """sha1 of a local file, reusing the cached one when size and mtime did not change."""
    st = path.stat()
    if isinstance(cached, dict) and cached.get("size") == st.st_size and cached.get("mtime_ns") == st.st_mtime_ns:
        digest = str(cached["sha1"])
    else:
        digest = _sha1(path)
    return digest, {"sha1": digest, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _cached_sha1(cached: object) -> str | None:
    if isinstance(cached, dict):
        return cached.get("sha1")
    return cached if isinstance(cached, str) else None  # pre-mtime cache format


def _excluded(rel: str, patterns: list[str]) -> bool:
    parts = rel.replace("\\", "/").split("/")
    return any(fnmatch.fnmatch(p, pat) for pat in patterns for p in parts)


def iter_local(local: Path, excludes: list[str]):
    if local.is_file():
        yield local, local.name
        return
    for root, dirs, files in os.walk(local):
        rel_root = os.path.relpath(root, local)
        dirs[:] = [d for d in dirs if not _excluded(os.path.join(rel_root, d), excludes)]
        for name in files:
            rel = os.path.normpath(os.path.join(rel_root, name))
            if not _excluded(rel, excludes):
                yield Path(root) / name, rel


def deploy(
    xbox: XboxConsole,
    local_path: str,
    remote_dir: str,
    *,
    excludes: list[str] | None = None,
    force: bool = False,
    prune: bool = False,
    progress=None,
) -> dict:
    """Sync local_path into remote_dir. prune=True also deletes console files that no longer
    exist locally (careful: that includes files the title itself wrote there, e.g. saves)."""
    local = Path(local_path).expanduser().resolve()
    if not local.exists():
        raise FileNotFoundError(f"Local path not found: {local}")
    if prune and not local.is_dir():
        raise ValueError("prune needs a local folder, not a single file")
    remote_root = normalize_path(remote_dir)
    patterns = DEFAULT_EXCLUDES if excludes is None else excludes
    cache = _load_cache()
    started = time.time()

    remote_sizes: dict[str, int] = {}
    remote_names: dict[str, str] = {}
    if xbox.exists(remote_root):
        for full, e in xbox.walk(remote_root):
            if not e["is_dir"]:
                remote_sizes[full.lower()] = e["size"]
                remote_names[full.lower()] = full
    else:
        xbox.makedirs(remote_root)

    uploaded, skipped, total_bytes = [], 0, 0
    made_dirs: set[str] = set()
    seen: set[str] = set()
    try:
        for path, rel in iter_local(local, patterns):
            remote = join_console(remote_root, rel)
            seen.add(remote.lower())
            key = f"{xbox.host}|{remote.lower()}"
            entry = cache.get(key)
            digest, new_entry = _local_digest(path, entry)
            size = new_entry["size"]
            same_remote = remote_sizes.get(remote.lower()) == size
            if not force and same_remote and _cached_sha1(entry) == digest:
                cache[key] = new_entry
                skipped += 1
                continue
            parent = remote.rpartition("\\")[0]
            if parent.lower() not in made_dirs and parent.lower() != remote_root.lower():
                xbox.makedirs(parent)
                made_dirs.add(parent.lower())
            xbox.upload_file(remote, path)
            cache[key] = new_entry
            uploaded.append(rel.replace("\\", "/"))
            total_bytes += size
            if progress:
                progress(rel, size)

        deleted: list[str] = []
        if prune:
            for low, full in sorted(remote_names.items()):
                if low not in seen:
                    xbox.delete(full)
                    cache.pop(f"{xbox.host}|{low}", None)
                    deleted.append(full[len(remote_root) :].lstrip("\\").replace("\\", "/"))
    finally:
        _save_cache(cache)  # keep progress even if an upload failed half-way
    elapsed = time.time() - started
    out = {
        "remote_dir": remote_root,
        "uploaded": uploaded,
        "uploaded_count": len(uploaded),
        "skipped_unchanged": skipped,
        "bytes_sent": total_bytes,
        "seconds": round(elapsed, 2),
    }
    if prune:
        out["deleted"] = deleted
    return out
