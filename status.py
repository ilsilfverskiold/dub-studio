"""Read-only reporting: per-clip status, measured level envelopes, voice names."""

import hashlib
import os

import numpy as np

import pipeline as pl
import providers as pv
from audio_engine import SR
from bus import STATE
from session import _final_path, _master_settings

_VOICES_CACHE = {"t": 0, "voices": []}


def _resolve_target(b):
    """Optional {"clip": name} -> ([path], name) | (None, name-if-unknown) | (None, None)."""
    name = os.path.basename(b.get("clip", "") or "")
    if not name:
        return None, None
    clip = next((c for c in STATE["clips"] if os.path.basename(c) == name), None)
    return ([clip] if clip else None), name


def _clip_by_name(b):
    name = os.path.basename(b.get("clip", ""))
    return next((c for c in STATE["clips"] if os.path.basename(c) == name), None)


def _voice_name(vid):
    for v in _VOICES_CACHE["voices"]:
        if v.get("voice_id") == vid:
            return v.get("name")
    try:
        for v in pv.el_voices():
            if v.get("voice_id") == vid:
                return v.get("name")
    except Exception:
        pass
    return (vid or "")[:10] + "…"



_LEVEL_CACHE = {}                      # path -> (mtime, points) — measured RMS envelope


def _level_env(path, pps=25):
    """The timeline's level lane: real measured RMS of the RENDERED audio, ~40ms windows,
    dB clamped to [-60, 0]. What you see is what you hear — never a decorative waveform."""
    a = pv.read_audio(path)
    mono = np.mean(a ** 2, axis=0)
    hop = SR // pps
    n = len(mono) // hop
    if n < 2:
        return []
    win = mono[:n * hop].reshape(n, hop).mean(axis=1)
    env = np.clip(10.0 * np.log10(win + 1e-12), -60.0, 0.0)
    return [round(float(v), 1) for v in env]


def _level_points(fpath):
    mt = os.path.getmtime(fpath)
    hit = _LEVEL_CACHE.get(fpath)
    if not hit or hit[0] != mt:
        try:
            _LEVEL_CACHE[fpath] = (mt, _level_env(fpath))
        except Exception:
            _LEVEL_CACHE[fpath] = (mt, [])
    return _LEVEL_CACHE[fpath][1]



# ------------------------------------------------ status

def _tts_take_stale(t, who, cast):
    """True when a generated take no longer matches the window's current text or the
    character's CURRENT voice (the file name hashes text+voice at generation time).
    Mirrors /api/tts-retake's resolution: the project cast is the voice authority."""
    if not t.get("file") or not t.get("character"):
        return False
    ident = pl._norm_id(t["character"])
    vid = (cast.get(ident) or {}).get("voice_id") or \
          {pl._norm_id(k): v for k, v in ((who or {}).get("voice_of") or {}).items()}.get(ident)
    if not vid:
        return False
    return hashlib.sha1(((t.get("text") or "") + vid).encode()).hexdigest()[:8] not in t["file"]


def _clip_status(c):
    d = pl.clip_state_dir(c)
    regions, voices, ana = pl.effective_detection(c)
    who = pl._read_json(os.path.join(d, "who.json"))
    meta = pl._read_json(os.path.join(d, "meta.json"))
    edits = pl._read_json(os.path.join(d, "edits.json")) or {}
    master = _master_settings()
    cast = {pl._norm_id(x["identifier"]): x for x in pl.load_cast()}
    chars = []
    if who:
        for ch in who.get("chars", []):
            cc = cast.get(pl._norm_id(ch["identifier"]), {})
            chars.append({**ch, "identifier": pl._norm_id(ch["identifier"]),
                          "voice_id": cc.get("voice_id", "")})
    takes = pl.load_takes(c)
    out = {
        "name": os.path.basename(c),
        "stage": pl.clip_stage(c),
        "duration": (ana or {}).get("duration"),
        "voices": voices,
        "regions": regions,
        "chars": chars,
        "assigns": (who or {}).get("assigns", []),
        "kinds": [a.get("kind") for a in (who or {}).get("assigns", [])],
        "est": {"el_usd": (ana or {}).get("el_usd", 0), "gemini_usd": (ana or {}).get("gemini_usd", 0),
                "credits": (ana or {}).get("estimated_credits", 0)},
        "src": "/media/" + os.path.basename(c),
        "sensitivity": edits.get("sensitivity"),
        "duck_regions": edits.get("duck_regions", []),
        "duck_owned": bool(edits.get("duck_owned")),
        "duck_windows_owned": bool((meta or {}).get("rendered_duck_owned")),
        "voice_mutes": edits.get("voice_mutes", []),
        "voice_splits": edits.get("voice_splits", []),
        "boost_regions": edits.get("boost_regions", []),
        "tts_takes": [{"start": t["start"], "end": t["end"], "text": t.get("text", ""),
                       "file": t.get("file", ""), "speed": t.get("speed"),
                       "place_at": t.get("place_at"), "character": t.get("character", ""),
                       "has": bool(t.get("file")) and os.path.exists(
                           os.path.join(d, "tts", t.get("file", "") or "_")),
                       # the generated file's name hashes text+voice — if the character's
                       # CURRENT voice (or the window's edited text) no longer matches, the
                       # take is from a previous voice and the UI must say so, never lie
                       "voice_stale": _tts_take_stale(t, who, cast)}
                      for t in (edits.get("tts_takes") or [])],
        "duck_windows": (meta or {}).get("duck_windows", []),
        "chunks": (meta or {}).get("chunks", []),
        "preserves": (meta or {}).get("preserves", []),   # kept regions — original sound plays
        "keep_off": pl._keep_off_windows(c),              # Convert-off regions — original plays
        "voice_dips": edits.get("voice_dips", []),        # original-voice layer turn-downs
        "mix_override": pl.get_clip_override(c) is not None,
        "mix_settings": pl.effective_settings(c, master).to_dict(),
        "mix_stale": pl.mix_stale(c, master),
        "takes": len(takes.get("takes", [])),
        "starred_take": takes.get("starred"),
    }
    fin = _final_path(c)
    if os.path.exists(fin):
        h = pv.file_hash(c)
        out["final"] = "/media/out/" + os.path.basename(fin)
        out["stale"] = not bool(meta)     # result exists but no longer matches the chosen settings/voice
        if os.path.exists(os.path.join(d, "solo_voice.wav")):
            out["solo_voice"] = f"/media/solo/{h}/voice"
            out["solo_bed"] = f"/media/solo/{h}/bed"
    return out


