"""Take history: every re-convert archives the previous result; pins survive."""

import os
import shutil
import time

from .cast import _norm_id
from .store import _read_json, _write_json, clip_state_dir


def takes_dir(clip_path):
    p = os.path.join(clip_state_dir(clip_path), "takes")
    os.makedirs(p, exist_ok=True)
    return p


def load_takes(clip_path):
    return _read_json(os.path.join(takes_dir(clip_path), "takes.json")) or {"takes": [], "starred": None}


def _save_takes(clip_path, t):
    _write_json(os.path.join(takes_dir(clip_path), "takes.json"), t)


def archive_take(clip_path, reason, final_path):
    """Copy the current final into the take history. Keeps the 3 newest unstarred takes;
    a starred take is never rotated out."""
    if not os.path.exists(final_path):
        return None
    t = load_takes(clip_path)
    ms = int(time.time() * 1000)
    tid, fn = f"t_{ms}", f"take_{ms}.mp4"
    shutil.copyfile(final_path, os.path.join(takes_dir(clip_path), fn))
    d = clip_state_dir(clip_path)
    meta = _read_json(os.path.join(d, "meta.json")) or {}
    who = _read_json(os.path.join(d, "who.json")) or {}
    row = {"id": tid, "file": fn, "created": time.time(), "reason": reason,
           "settings": meta.get("rendered_with"),
           "voices": {_norm_id(k): v for k, v in (who.get("voice_of") or {}).items()}}
    # archive the VOICE TRACK too: pinning a take later means "use THIS voice performance,
    # re-mix and clean everything else with the CURRENT rules" — only possible with the stem
    vr = os.path.join(d, "voice_raw.wav")
    if os.path.exists(vr):
        vfn = f"take_{ms}_voice.wav"
        shutil.copyfile(vr, os.path.join(takes_dir(clip_path), vfn))
        row["voice_file"] = vfn
        row["chunks"] = meta.get("chunks", [])
    t["takes"].insert(0, row)
    keep, unstarred = [], 0
    for tk in t["takes"]:
        if tk["id"] == t.get("starred"):
            keep.append(tk)
        elif unstarred < 3:
            keep.append(tk)
            unstarred += 1
        else:
            for key in ("file", "voice_file"):
                if tk.get(key):
                    fp = os.path.join(takes_dir(clip_path), tk[key])
                    if os.path.exists(fp):
                        os.remove(fp)
    t["takes"] = keep
    _save_takes(clip_path, t)
    return tid


def star_take(clip_path, take_id, final_path):
    """Pin a take. With an archived voice track: that VOICE is used and the mix rebuilds
    around it with the current rules (caller re-renders, free). Without one (old takes):
    the flattened file plays as-is. Returns True if the pin carries a voice track."""
    t = load_takes(clip_path)
    tk = next((x for x in t["takes"] if x["id"] == take_id), None)
    if not tk:
        raise RuntimeError("unknown take")
    has_stem = bool(tk.get("voice_file")) and \
        os.path.exists(os.path.join(takes_dir(clip_path), tk["voice_file"]))
    if not has_stem:
        shutil.copyfile(os.path.join(takes_dir(clip_path), tk["file"]), final_path)
    t["starred"] = take_id
    _save_takes(clip_path, t)
    return has_stem


def clear_star(clip_path):
    t = load_takes(clip_path)
    if t.get("starred"):
        t["starred"] = None
        _save_takes(clip_path, t)


def delete_take(clip_path, take_id):
    t = load_takes(clip_path)
    if t.get("starred") == take_id:
        raise RuntimeError("that take is starred — unstar it first")
    tk = next((x for x in t["takes"] if x["id"] == take_id), None)
    if not tk:
        raise RuntimeError("unknown take")
    fp = os.path.join(takes_dir(clip_path), tk["file"])
    if os.path.exists(fp):
        os.remove(fp)
    t["takes"] = [x for x in t["takes"] if x["id"] != take_id]
    _save_takes(clip_path, t)


