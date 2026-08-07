"""Detection (free, local): analyze, user edits to WHEN, re-derive at a sensitivity."""

import os

import providers as pv
from audio_engine import SR

from .store import _read_json, _write_json, clip_state_dir


def analyze(clip_path, emit):
    """Free, local: duration, diarization, separation. Returns the plan + an honest cost estimate."""
    emit("detect", f"listening to {os.path.basename(clip_path)} for voices — nothing is uploaded, "
         "this model runs on your machine",
         detail="pyannote speaker diarization · local · free")
    dur = pv.media_duration(clip_path) or 8.0
    regions, speakers = pv.diarize(clip_path, emit=lambda s, m, level="info", detail=None: emit(s, m, level=level, detail=detail))
    emit("diarize", f"found {len(regions)} speech region(s) from {len(speakers)} distinct "
         f"voice-print{'s' if len(speakers) != 1 else ''} — these are the blocks on the timeline",
         metrics={"regions": regions, "voices": len(speakers)},
         detail="a region = one stretch of continuous speech; a voice-print = one distinct speaker sound")
    inst, voc = pv.separate(clip_path, emit=lambda s, m, level="info", detail=None: emit(s, m, level=level, detail=detail))
    voiced_s = sum(r["end"] - r["start"] for r in regions)

    # cost model (approximate). ElevenLabs bills ~1000 credits per minute of audio for isolation and
    # for speech-to-speech alike (~$0.20 per 1000 credits on typical paid plans). Gemini identity is
    # roughly $0.02 per clip. Shown per provider, and CACHE-AWARE: steps whose exact input is already
    # cached cost nothing, and the estimate must say so instead of quoting full price.
    d = clip_state_dir(clip_path)
    src_wav = os.path.join(d, "src.wav")
    pv.ff(["-y", "-i", clip_path, "-vn", "-ac", "2", "-ar", str(SR), src_wav], check=True)
    iso_cached = os.path.exists(os.path.join(pv.CACHE, "iso", pv.file_hash(src_wav) + ".wav"))
    prior_run = os.path.exists(os.path.join(d, "meta.json"))

    iso_cr = 0 if (iso_cached or not regions) else int(dur / 60 * 1000)
    sts_cr = int((dur if len(speakers) <= 1 else voiced_s) / 60 * 1000) if regions else 0
    if prior_run:
        sts_cr = 0        # this exact clip was fully converted before; its conversions are cached
    el_usd = round((iso_cr + sts_cr) * 0.0002, 2)
    gem_usd = 0.02 if regions else 0.0
    meta = {"clip": clip_path, "duration": round(dur, 2), "regions": regions,
            "voices": len(speakers), "instrumental": inst, "vocals": voc,
            "estimated_credits": iso_cr + sts_cr, "iso_credits": iso_cr, "sts_credits": sts_cr,
            "el_usd": el_usd, "gemini_usd": gem_usd,
            "iso_cached": iso_cached, "prior_run": prior_run}
    _write_json(os.path.join(d, "analysis.json"), meta)
    # Stage-appropriate logging: at detect time the next step is CASTING, so that is the only
    # cost mentioned here. The conversion estimate is logged when casting completes — the moment
    # Convert actually becomes the next step (the numbers are stored for the UI either way).
    if not regions:
        emit("analyze", "no voice detected — this clip costs nothing to process", level="metric")
    elif prior_run:
        emit("analyze", "this exact clip was processed before — its conversion is cached, "
             "so nothing re-bills after casting", level="metric")
    else:
        emit("analyze", f"detected — next step is Cast (≈ ${gem_usd:.2f}, Gemini watches the clip once); "
             "the conversion price is shown once the cast is set",
             level="metric", metrics={"el_credits": iso_cr + sts_cr, "el_usd": el_usd,
                                      "gemini_usd": gem_usd})
    return meta




def save_detection_edits(clip_path, regions=None, voices=None):
    """Store user edits to WHEN. The CAST SURVIVES — only the conversion becomes stale; region
    assignments are re-synced by index so who-speaks-where is kept wherever possible."""
    d = clip_state_dir(clip_path)
    ed = _read_json(os.path.join(d, "edits.json")) or {}
    if regions is not None:
        ed["regions"] = [{"start": round(float(r["start"]), 2), "end": round(float(r["end"]), 2)}
                         for r in regions if float(r["end"]) > float(r["start"])]
        ed["regions"].sort(key=lambda r: r["start"])
    if voices is not None:
        ed["voices"] = int(voices)
    _write_json(os.path.join(d, "edits.json"), ed)
    who_p = os.path.join(d, "who.json")
    who = _read_json(who_p)
    if who and regions is not None:
        n = len(ed["regions"])
        default_char = who["chars"][0]["identifier"] if who.get("chars") else ""
        assigns = who.get("assigns", [])[:n]
        assigns += [{"character": default_char, "kind": "speech"}] * (n - len(assigns))
        who["assigns"] = assigns
        _write_json(who_p, who)
    mp = os.path.join(d, "meta.json")
    if os.path.exists(mp):
        os.remove(mp)


def postprocess_regions(raw_regions, sensitivity):
    """Re-derive regions from the CACHED raw diarization — no pyannote re-run, free.
    Honest semantics: this is a granularity control. High sensitivity keeps every raw turn;
    low sensitivity merges same-speaker turns across gaps and drops slivers. It can only
    merge/drop turns pyannote emitted — never surface ones it didn't."""
    s = max(0, min(100, int(sensitivity))) / 100.0
    merge_gap = 1.0 - 0.9 * s              # 1.0s @ 0  ->  0.1s @ 100
    min_len = 0.8 * (1.0 - s)              # 0.8s @ 0  ->  0.0s @ 100
    merged = []
    for r in sorted(raw_regions, key=lambda r: r["start"]):
        if merged and r.get("speaker") == merged[-1].get("speaker") \
                and r["start"] - merged[-1]["end"] <= merge_gap:
            merged[-1]["end"] = max(merged[-1]["end"], r["end"])
        else:
            merged.append(dict(r))
    kept = [r for r in merged if (r["end"] - r["start"]) >= min_len] or merged
    voices = len({r.get("speaker") for r in kept if r.get("speaker")}) or 1
    return ([{"start": round(r["start"], 2), "end": round(r["end"], 2)} for r in kept], voices)


def redetect(clip_path, sensitivity):
    ana = _read_json(os.path.join(clip_state_dir(clip_path), "analysis.json"))
    if not ana:
        raise RuntimeError("detect first")
    regions, voices = postprocess_regions(ana.get("regions") or [], sensitivity)
    save_detection_edits(clip_path, regions=regions, voices=voices)
    ep = os.path.join(clip_state_dir(clip_path), "edits.json")
    ed = _read_json(ep) or {}
    ed["sensitivity"] = int(sensitivity)
    _write_json(ep, ed)
    return regions, voices


