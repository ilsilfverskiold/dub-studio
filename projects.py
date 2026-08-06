"""Projects for Dub Studio — each project is a fully self-contained folder.

projects/
  projects.json          {"active": pid, "order": [pid, ...]}
  <pid>/project.json     name, created, clip_order, settings (master mix), reel_meta
  <pid>/uploads/         source clips
  <pid>/state/           cast.json + per-clip content-hash dirs
  <pid>/output/          finals + reel.mp4
  <pid>/cache/           iso/sts/sep paid-result caches (per-project by user decision:
                         projects never share results; re-using a clip elsewhere re-bills,
                         and the approval gate still fronts every paid run)

A pre-v2 flat session (uploads/state/output/cache at the repo root) is MOVED — never copied,
never deleted — into the first project on boot by migrate_legacy().
"""

import json
import os
import shutil
import time
import uuid

from audio_engine import Settings

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.path.join(HERE, "projects")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")


def _registry_path():
    return os.path.join(PROJECTS, "projects.json")


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=2)
    os.replace(tmp, path)


def project_dir(pid):
    return os.path.join(PROJECTS, pid)


def read_meta(pid):
    return json.load(open(os.path.join(project_dir(pid), "project.json")))


def write_meta(pid, meta):
    _atomic_write(os.path.join(project_dir(pid), "project.json"), meta)


def _default_meta(pid, name):
    return {"id": pid, "name": name, "created": time.time(), "clip_order": [],
            "settings": Settings().to_dict(),
            "reel_meta": {"rendered_at": None, "clip_order": [], "settings_hash": ""}}


def load_registry():
    """Registry, creating it (with an empty first project) if this is a fresh install."""
    p = _registry_path()
    if os.path.exists(p):
        reg = json.load(open(p))
        # heal: active must exist and be listed
        reg["order"] = [pid for pid in reg.get("order", []) if os.path.isdir(project_dir(pid))]
        if reg.get("active") not in reg["order"]:
            reg["active"] = reg["order"][0] if reg["order"] else None
        if reg["active"] is None:
            pid = create("Project 1")
            reg = json.load(open(p))
        return reg
    os.makedirs(PROJECTS, exist_ok=True)
    pid = _new_pid()
    _make_dirs(pid)
    write_meta(pid, _default_meta(pid, "Project 1"))
    reg = {"active": pid, "order": [pid]}
    _atomic_write(p, reg)
    return reg


def save_registry(reg):
    _atomic_write(_registry_path(), reg)


def _new_pid():
    return "p_" + uuid.uuid4().hex[:8]


def _make_dirs(pid):
    for sub in ("uploads", "state", "output", "cache"):
        os.makedirs(os.path.join(project_dir(pid), sub), exist_ok=True)


def create(name):
    reg = json.load(open(_registry_path())) if os.path.exists(_registry_path()) else {"active": None, "order": []}
    pid = _new_pid()
    _make_dirs(pid)
    write_meta(pid, _default_meta(pid, (name or "").strip() or f"Project {len(reg['order']) + 1}"))
    reg["order"].append(pid)
    if not reg.get("active"):
        reg["active"] = pid
    save_registry(reg)
    return pid


def rename(pid, name):
    meta = read_meta(pid)
    meta["name"] = (name or "").strip() or meta["name"]
    write_meta(pid, meta)


def delete(pid):
    """Removes the project folder entirely. Caller decides what becomes active."""
    reg = load_registry()
    shutil.rmtree(project_dir(pid), ignore_errors=True)
    reg["order"] = [x for x in reg["order"] if x != pid]
    if reg.get("active") == pid:
        reg["active"] = reg["order"][0] if reg["order"] else None
    save_registry(reg)
    if reg["active"] is None:
        return create("Project 1")
    return reg["active"]


def list_all():
    reg = load_registry()
    out = []
    for pid in reg["order"]:
        try:
            meta = read_meta(pid)
        except (OSError, json.JSONDecodeError):
            continue
        up = os.path.join(project_dir(pid), "uploads")
        clips = [f for f in os.listdir(up)] if os.path.isdir(up) else []
        clips = [f for f in clips if f.lower().endswith(VIDEO_EXTS)]
        out.append({"id": pid, "name": meta.get("name", pid), "created": meta.get("created", 0),
                    "clips": len(clips), "active": pid == reg["active"],
                    "reel_rendered": bool((meta.get("reel_meta") or {}).get("rendered_at"))})
    return out


def activate(pid):
    """Point pipeline + providers at this project and mark it active. Returns project.json."""
    import pipeline as pl
    import providers as pv
    d = project_dir(pid)
    if not os.path.isdir(d):
        raise RuntimeError("unknown project")
    _make_dirs(pid)
    pl.set_project_dirs(os.path.join(d, "state"), os.path.join(d, "output"))
    pv.set_cache_dir(os.path.join(d, "cache"))
    reg = load_registry()
    if reg.get("active") != pid:
        reg["active"] = pid
        save_registry(reg)
    return read_meta(pid)


def migrate_legacy():
    """Move a pre-v2 flat session (repo-root uploads/state/output/cache) into the first project.
    Idempotent: keyed solely on the absence of projects/projects.json. Data is MOVED with
    os.rename (same volume, atomic per dir) — never copied, never deleted."""
    if os.path.exists(_registry_path()):
        return None
    legacy = [d for d in ("uploads", "state", "output", "cache")
              if os.path.isdir(os.path.join(HERE, d)) and os.listdir(os.path.join(HERE, d))]
    if not legacy:
        return None                       # fresh install — load_registry() creates Project 1
    os.makedirs(PROJECTS, exist_ok=True)
    pid = _new_pid()
    os.makedirs(project_dir(pid), exist_ok=True)
    for sub in ("uploads", "state", "output", "cache"):
        src = os.path.join(HERE, sub)
        dst = os.path.join(project_dir(pid), sub)
        if os.path.isdir(src):
            try:
                os.rename(src, dst)
            except OSError:
                shutil.move(src, dst)
        else:
            os.makedirs(dst, exist_ok=True)
    up = os.path.join(project_dir(pid), "uploads")
    clip_order = sorted(f for f in os.listdir(up) if f.lower().endswith(VIDEO_EXTS))
    meta = _default_meta(pid, "Project 1")
    meta["clip_order"] = clip_order
    write_meta(pid, meta)
    _atomic_write(_registry_path(), {"active": pid, "order": [pid]})
    return pid
