"""Per-clip mix overrides on top of the master, and the mix-stale law."""

import os

import providers as pv
from audio_engine import Settings

from .store import (MIX_LAW, _read_json, _write_json, clip_state_dir,
                    effective_detection)
from .takes import load_takes


def get_clip_override(clip_path):
    """The clip's own mix settings, or None = 'match master'."""
    j = _read_json(os.path.join(clip_state_dir(clip_path), "mix.json"))
    return (j or {}).get("override")


def set_clip_override(clip_path, settings_dict):
    """settings_dict = full knob dict for this clip; None deletes the override (Match master)."""
    p = os.path.join(clip_state_dir(clip_path), "mix.json")
    if settings_dict is None:
        if os.path.exists(p):
            os.remove(p)
    else:
        _write_json(p, {"override": Settings.from_dict(settings_dict).to_dict()})


def effective_settings(clip_path, master):
    """master ⊕ per-clip override. reel_lufs is master-only — a clip cannot move the program's
    delivery loudness."""
    d = master.to_dict()
    o = get_clip_override(clip_path)
    if o:
        d.update(o)
        d["reel_lufs"] = master.to_dict().get("reel_lufs", d.get("reel_lufs"))
    return Settings.from_dict(d)


def _keep_off_windows(clip_path):
    """Regions whose visible Convert field is OFF — they play the original sound at mix time.
    Flipping the field is always a FREE mix change (the paid conversion stays cached)."""
    who = _read_json(os.path.join(clip_state_dir(clip_path), "who.json")) or {}
    regions, _, _ = effective_detection(clip_path)
    asn = who.get("assigns", [])
    return [[round(float(r["start"]), 2), round(float(r["end"]), 2)]
            for i, r in enumerate(regions) if i < len(asn) and asn[i].get("convert") is False]


def mix_stale(clip_path, master):
    """True when the final on disk was mixed with different settings than would apply now.
    (Convert-staleness — voice/regions changed — is a separate flag.)"""
    meta = _read_json(os.path.join(clip_state_dir(clip_path), "meta.json"))
    if not meta:
        return False
    _t = load_takes(clip_path)
    if _t.get("starred"):
        _stk = next((x for x in _t["takes"] if x["id"] == _t["starred"]), None)
        if not (_stk and _stk.get("voice_file")):
            return False                   # stemless old pin: the flattened file plays as-is
        # a pin WITH a voice track re-mixes live — normal staleness applies below
    ed = _read_json(os.path.join(clip_state_dir(clip_path), "edits.json")) or {}
    if meta.get("mix_law") != MIX_LAW:
        return True                        # rendered under an older mix LAW — one free render heals
    if f"_{pv.sep_model_key()}_" not in os.path.basename(meta.get("instrumental") or ""):
        return True                        # separation model changed — free re-split + re-render
    if "duck_windows" not in meta:
        return True                        # rendered before duck mapping existed — one free render heals
    if meta.get("rendered_duck", []) != ed.get("duck_regions", []) or \
            bool(meta.get("rendered_duck_owned")) != bool(ed.get("duck_owned")):
        return True                        # duck map changed since this mix
    if meta.get("rendered_voice_mutes", []) != ed.get("voice_mutes", []):
        return True                        # voice mutes changed since this mix
    if meta.get("rendered_boost", []) != ed.get("boost_regions", []):
        return True                        # boost regions changed since this mix
    if meta.get("rendered_dips", []) != (ed.get("voice_dips", []) or []):
        return True                        # original-voice turn-downs changed since this mix
    if meta.get("rendered_tts", []) != (ed.get("tts_takes", []) or []):
        return True                        # TTS retakes changed since this mix
    if meta.get("rendered_keep_off", []) != _keep_off_windows(clip_path):
        return True                        # a region's Convert field flipped — free re-mix applies it
    rw = meta.get("rendered_with")
    # normalize through Settings so a mix rendered before a knob EXISTED (and therefore at its
    # default) is not flagged stale by the mere addition of that knob
    return rw is None or Settings.from_dict(rw).to_dict() != effective_settings(clip_path, master).to_dict()


