"""The stage runners — every paid or heavy operation the Handler queues."""

import os
import time

import bus
import pipeline as pl
from bus import STATE, _clip_emit, emit
from session import (_final_path, _master_settings, _master_state, _save_project,
                     _settings_hash)


def run_analyze(targets=None):
    todo = [c for c in (targets or STATE["clips"])
            if not pl._read_json(os.path.join(pl.clip_state_dir(c), "analysis.json"))]
    if not todo:
        emit("", "detect", "all clips are already detected — nothing to do", level="ok")
        return
    for c in todo:
        pl.analyze(c, _clip_emit(c))
    emit("", "detect", f"{len(todo)} clip(s) detected — review regions and voice count, then Cast",
         level="ok")


def run_identify(targets=None):
    n = 0
    for c in (targets or STATE["clips"]):
        d = pl.clip_state_dir(c)
        if not pl._read_json(os.path.join(d, "analysis.json")):
            pl.analyze(c, _clip_emit(c))
        if not pl._read_json(os.path.join(d, "who.json")):
            pl.identify_clip(c, _clip_emit(c))
            n += 1
    if n == 0:
        emit("", "cast", "already cast — nothing to do", level="ok")
    else:
        usd = credits = 0
        for c in (targets or STATE["clips"]):
            ana = pl._read_json(os.path.join(pl.clip_state_dir(c), "analysis.json")) or {}
            usd += ana.get("el_usd", 0)
            credits += ana.get("estimated_credits", 0)
        emit("", "cast", f"cast ready ({n} clip(s)) — audition the voices, swap any you don't like, "
             f"then Convert ≈ ${usd:.2f} (≈ {credits} ElevenLabs credits; cached steps are free)",
             level="ok")


def run_convert(targets=None):
    master = _master_settings()
    if targets:
        todo = targets                                    # explicitly asked — run even if converted
    else:
        todo = [c for c in STATE["clips"] if pl.clip_stage(c) != "converted"]
        skipped = len(STATE["clips"]) - len(todo)
        if skipped:
            emit("", "convert", f"skipping {skipped} already-converted clip(s) — nothing re-bills",
                 level="cache")
    if not todo:
        emit("", "convert", "everything is already converted — nothing to do", level="ok")
        return
    for c in todo:
        d = pl.clip_state_dir(c)
        if not pl._read_json(os.path.join(d, "analysis.json")):
            pl.analyze(c, _clip_emit(c))
        if not pl._read_json(os.path.join(d, "who.json")):
            pl.identify_clip(c, _clip_emit(c))
        pl.convert_clip(c, _clip_emit(c), pl.effective_settings(c, master))
    _rebuild_reel(master)
    emit("", "done", "converted — listen below; the sound panel re-mixes for free", level="ok")


def _rebuild_reel(master):
    finals = [_final_path(c) for c in STATE["clips"]
              if pl.clip_stage(c) == "converted" and os.path.exists(_final_path(c))]
    if len(finals) > 1:
        STATE["reel"] = pl.make_reel(finals, master, _clip_emit(""))
    _save_project(reel_meta={"rendered_at": time.time(),
                             "clip_order": [os.path.basename(c) for c in STATE["clips"]],
                             "settings_hash": _settings_hash(STATE["settings"])})


def run_render_master():
    """Free: every clip whose mix is out of date is re-mixed with its EFFECTIVE settings
    (master ⊕ its own override), then the program is re-stitched. Nothing re-converts."""
    if _master_state()["state"] == "ok":
        emit("", "done", "the master is already current — no fader moved and no clip changed, "
             "so there is nothing to render", level="ok")
        return
    master = _master_settings()
    n = 0
    for c in STATE["clips"]:
        if not pl._read_json(os.path.join(pl.clip_state_dir(c), "meta.json")):
            continue
        if pl.mix_stale(c, master) or not os.path.exists(_final_path(c)):
            pl.remix(c, pl.effective_settings(c, master), _clip_emit(c))
            n += 1
    _rebuild_reel(master)
    emit("", "done", f"master rendered — {n} clip mix(es) refreshed, no credits spent", level="ok")


def run_remix_one(clip):
    master = _master_settings()
    if pl._read_json(os.path.join(pl.clip_state_dir(clip), "meta.json")):
        pl.remix(clip, pl.effective_settings(clip, master), _clip_emit(clip))
    emit("", "done", "re-mix complete (no credits spent) — the master is stale until you re-render it",
         level="ok")



bus.set_remix_runner(run_remix_one)
