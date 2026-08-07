"""The active project: open/save lifecycle, master settings, master staleness."""

import hashlib
import json
import os

import pipeline as pl
import projects
from audio_engine import Settings
from bus import STATE, _load_log_history, _pid, emit

UPLOADS = None                     # set by _open_project()


def _final_path(clip):
    return os.path.join(pl.OUTPUT, os.path.splitext(os.path.basename(clip))[0] + "_final.mp4")



def _master_settings():
    return Settings.from_dict(STATE["settings"])


def _settings_hash(d):
    return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


def _emit_settings_diff(clip, old, new, warn_if_same=True):
    """Log exactly which knobs changed, old -> new — computed from what is actually stored,
    so the log doubles as proof the change took. Returns True if anything changed."""
    diffs = [f"{k} {old.get(k)} → {v}" for k, v in new.items() if old.get(k) != v]
    if diffs:
        emit(clip, "mix", "settings changed: " + " · ".join(diffs),
             level="info", detail="every mix from now on renders with these values — old takes keep theirs")
    elif warn_if_same:
        emit(clip, "mix", "no setting actually changed — the faders match what's already rendered",
             level="warn")
    return bool(diffs)



# ------------------------------------------------ project lifecycle

def _open_project(pid):
    global UPLOADS
    meta = projects.activate(pid)
    UPLOADS = os.path.join(projects.project_dir(pid), "uploads")
    os.makedirs(UPLOADS, exist_ok=True)
    files = {f: os.path.join(UPLOADS, f) for f in sorted(os.listdir(UPLOADS))
             if f.lower().endswith(projects.VIDEO_EXTS)}
    order = [f for f in meta.get("clip_order", []) if f in files]
    order += [f for f in sorted(files) if f not in order]
    STATE["clips"] = [files[f] for f in order]
    STATE["settings"] = {**Settings().to_dict(), **(meta.get("settings") or {})}
    STATE["reel"] = None
    STATE["project"] = {"id": pid, "name": meta.get("name", pid)}
    _load_log_history(pid)
    emit("", "project", f"opened \"{meta.get('name')}\" — {len(order)} clip(s)", level="ok")
    # LAW MIGRATION (once per project): the Background fader became an ABSOLUTE level — values
    # stored under the old meaning must never be silently reinterpreted. Reset to equal-the-
    # voice and say so; the user sets it where they want from there.
    if (meta.get("mix_migrated") or 0) < 4:   # the law-4 fader re-meaning, once per project EVER
        old_bed = STATE["settings"].get("bed_db")
        STATE["settings"]["bed_db"] = STATE["settings"].get("dialog_db", -16.0)
        for c in STATE["clips"]:
            ov = pl.get_clip_override(c)
            if ov is not None:
                ov["bed_db"] = ov.get("dialog_db", STATE["settings"]["bed_db"])
                pl.set_clip_override(c, ov)
        meta["mix_migrated"] = pl.MIX_LAW
        meta["settings"] = STATE["settings"]
        projects.write_meta(pid, meta)
        emit("", "mix", "the Background fader now means an ABSOLUTE level (same scale as the "
             f"voice) — reset from {old_bed} to {STATE['settings']['bed_db']:.1f}, equal to the "
             "voice fader. Move it where you want it; the number is the measured level",
             level="warn")
    # STALE CHECK on open — flag only, never render here (auto-rendering on open stalled the
    # whole app). The master button lights up; ONE free click applies everything pending.
    # Edits the user makes still render themselves through the queue as always.
    master = _master_settings()
    behind = [c for c in STATE["clips"]
              if pl._read_json(os.path.join(pl.clip_state_dir(c), "meta.json"))
              and pl.mix_stale(c, master)]
    if behind:
        emit("", "mix", f"{len(behind)} clip(s) have sound updates pending — press "
             "\"Render master · free\" to apply them (no credits)", level="info")


def _save_project(reel_meta=None):
    pid = _pid()
    if pid == "_":
        return
    try:
        meta = projects.read_meta(pid)
    except (OSError, json.JSONDecodeError):
        return
    meta["clip_order"] = [os.path.basename(c) for c in STATE["clips"]]
    meta["settings"] = STATE["settings"]
    if reel_meta is not None:
        meta["reel_meta"] = reel_meta
    projects.write_meta(pid, meta)


def _reel_meta():
    try:
        return projects.read_meta(_pid()).get("reel_meta") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _master_state():
    """none: nothing to assemble yet. stale: something changed since the last render. ok: current."""
    master = _master_settings()
    reel = os.path.join(pl.OUTPUT, "reel.mp4")
    finals = [c for c in STATE["clips"] if os.path.exists(_final_path(c))]
    rm = _reel_meta()
    if not finals:
        return {"state": "none", "rendered_at": None}
    if not rm.get("rendered_at"):
        return {"state": "stale", "rendered_at": None}
    stale = False
    if rm.get("clip_order") != [os.path.basename(c) for c in STATE["clips"]]:
        stale = True
    if rm.get("settings_hash") != _settings_hash(STATE["settings"]):
        stale = True
    for c in STATE["clips"]:
        d = pl.clip_state_dir(c)
        if not pl._read_json(os.path.join(d, "meta.json")):
            stale = True                                   # convert-stale
        elif pl.mix_stale(c, master):
            stale = True
    if os.path.exists(reel):
        mt = [os.path.getmtime(_final_path(c)) for c in finals]
        if mt and max(mt) > os.path.getmtime(reel) + 1:
            stale = True
    elif len(finals) > 1:
        stale = True
    return {"state": "stale" if stale else "ok", "rendered_at": rm.get("rendered_at")}


