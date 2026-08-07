"""Convert (paid, cached): isolation + STS with the reviewed regions and cast."""

import os

import numpy as np

import audio_engine as ae
import providers as pv
from audio_engine import SR, Settings

from . import store
from .cast import _norm_id, load_cast
from .mix import remix
from .store import (_read_json, _write_json, clip_state_dir,
                    effective_detection)
from .takes import archive_take, clear_star


def convert_clip(clip_path, emit, settings=None):
    """Stage 4 (paid): isolation + STS using exactly the reviewed regions/voices/kinds/cast."""
    settings = settings or Settings()
    d = clip_state_dir(clip_path)
    # a re-convert supersedes the current take: archive it first (nothing is ever lost silently)
    prev_final = os.path.join(store.OUTPUT, os.path.splitext(os.path.basename(clip_path))[0] + "_final.mp4")
    if os.path.exists(prev_final):
        archive_take(clip_path, "reconvert", prev_final)
        clear_star(clip_path)
    meta = _read_json(os.path.join(d, "analysis.json"))
    if not meta:
        raise RuntimeError("analyze first")
    regions, n_voices, _ = effective_detection(clip_path)
    who = _read_json(os.path.join(d, "who.json"))
    if who is None:
        raise RuntimeError("identify the cast first")
    dur = meta["duration"]
    N = int(dur * SR)
    chars, assigns = who["chars"], who["assigns"]
    # normalize at the read boundary — old who.json files may hold "@name" keys
    voice_of = {_norm_id(k): v for k, v in (who.get("voice_of") or {}).items()}
    # the user's who-speaks-where assignments override the diarizer's voice count.
    # Only SPOKEN words make a speaker — singing is treated like laughter (kept, not re-voiced),
    # so a singing-only character must not flip the clip into the multi-voice path.
    speech_chars = {_norm_id(a.get("character")) for a in assigns
                    if a.get("kind") == "speech" and a.get("character")}
    if speech_chars:
        n_voices = len(speech_chars)
    cast = load_cast()
    # the PROJECT cast is the voice authority: it overrides who.json (voice swaps since
    # identify) AND covers characters never cast in this clip — anyone can speak anywhere
    for c in cast:
        voice_of[_norm_id(c["identifier"])] = c["voice_id"]

    if not regions or not chars:
        emit("convert", "no voice to convert — keeping the original audio", level="ok")
        meta.update({"mode": "no_voice", "chunks": [], "preserves": []})
        _write_json(os.path.join(d, "meta.json"), meta)
        return remix(clip_path, settings, emit)

    src_wav = os.path.join(d, "src.wav")
    if not os.path.exists(src_wav):
        pv.ff(["-y", "-i", clip_path, "-vn", "-ac", "2", "-ar", str(SR), src_wav], check=True)
    stem_path = pv.el_isolate(src_wav, emit=lambda s, m, level="info", detail=None: emit(s, m, level=level, detail=detail))
    stem = pv.read_audio(stem_path)

    canvas = np.zeros((2, N + SR), dtype=np.float32)
    chunks, preserves = [], []

    if n_voices <= 1:
        # ONE voice: whole stem -> one STS call -> aligned back to the timeline.
        vid = voice_of.get(_norm_id(chars[0]["identifier"])) if chars else None
        out = pv.el_sts(stem[:, :N], vid, emit=lambda s, m, level="info", detail=None: emit(s, m, level=level, detail=detail))
        in_ch = ae.voiced_chunks(stem[:, :N])
        out_ch = ae.voiced_chunks(out)
        if in_ch and len(in_ch) == len(out_ch):
            for (a0, a1), (b0, b1) in zip(in_ch, out_ch):
                seg = out[:, b0:b1]
                e = min(canvas.shape[1], a0 + seg.shape[1])
                canvas[:, a0:e] += seg[:, :e - a0]
                chunks.append([round(a0 / SR, 3), round(e / SR, 3)])
            emit("align", f"placed {len(in_ch)} voice burst(s) back at their original timestamps", level="ok",
                 detail="STS does not preserve silence placement — timing is re-imposed from the original audio")
        else:
            off = max(0, (in_ch[0][0] - out_ch[0][0]) if (in_ch and out_ch) else 0)
            e = min(canvas.shape[1], off + out.shape[1])
            canvas[:, off:e] += out[:, :e - off]
            chunks.append([round(off / SR, 3), round(e / SR, 3)])
            emit("align", f"burst counts differ ({len(in_ch)} in / {len(out_ch)} out) — "
                 "aligned first onset, placed whole take", level="warn")
        mode = "one_voice"
    else:
        emit("convert", f"{n_voices} speakers — converting region by region so each gets its own voice",
             level="info",
             detail="your who-speaks-where assignments route this — only spoken words are re-voiced; "
                    "laughter and singing only when 1.2 s or longer, everything else is kept")
        default_char = _norm_id(chars[0]["identifier"])
        PAD = int(0.20 * SR)
        MIN_WORDLESS_STS_S = 1.2
        for i, (r, a) in enumerate(zip(regions, assigns)):
            s0, e0 = int(r["start"] * SR), min(stem.shape[1], int(r["end"] * SR))
            lo = int(regions[i - 1]["end"] * SR) if i > 0 else 0
            hi = int(regions[i + 1]["start"] * SR) if i + 1 < len(regions) else stem.shape[1]
            s0p, e0p = max(lo, s0 - PAD), min(hi, e0 + PAD)
            # the region's own CONVERT field decides — set by the user, visible in the panel.
            # When absent (old data), the default follows the kind rules the user approved:
            # words always; laughter/singing only when 1.2 s or longer.
            if "convert" in a:
                convert_it = bool(a["convert"]) and e0 - s0 > int(0.1 * SR)
            else:
                convert_it = (a["kind"] == "speech" and e0 - s0 > int(0.1 * SR)) or \
                             (a["kind"] in ("human_nonword", "singing") and
                              (r["end"] - r["start"]) >= MIN_WORDLESS_STS_S)
            if convert_it:
                char = _norm_id(a.get("character")) if _norm_id(a.get("character")) in voice_of else default_char
                out = pv.el_sts(stem[:, s0p:e0p], voice_of[char],
                                emit=lambda s, m, level="info", detail=None: emit(s, m, level=level, detail=detail))
                e = min(canvas.shape[1], s0p + out.shape[1])
                canvas[:, s0p:e] += out[:, :e - s0p]
                chunks.append([round(s0p / SR, 3), round(e / SR, 3)])
                emit("convert", f"{s0p/SR:.2f}-{e0p/SR:.2f}s -> STS @{char}")
            else:
                preserves.append([r["start"], r["end"]])
                emit("convert", f"{r['start']:.2f}-{r['end']:.2f}s -> kept from the original ({a['kind']}) — "
                     "never sent to the voice model")
        mode = "multi_voice"

    if preserves:
        # engine OPINIONS become visible proposals (user law): a kept moment that measures
        # quiet next to the voice fader gets a PROPOSED green boost bar on the timeline —
        # movable, deletable, the user's. Never hidden gain.
        ep = os.path.join(d, "edits.json")
        ed_p = _read_json(ep) or {}
        bars = ed_p.get("boost_regions", []) or []
        added = 0
        for a, b in preserves:
            s0p, e0p = int(a * SR), min(stem.shape[1], int(b * SR))
            if e0p - s0p <= 0:
                continue
            seg_db = 20.0 * np.log10(ae.active_rms(stem[:, s0p:e0p]) + 1e-12)
            overlapped = any(not (bb["end"] <= a or bb["start"] >= b) for bb in bars)
            if seg_db < settings.dialog_db - 3.0 and not overlapped:
                bars.append({"start": round(float(a), 2), "end": round(float(b), 2)})
                added += 1
        if added:
            ed_p["boost_regions"] = bars
            _write_json(ep, ed_p)
            emit("mix", f"proposed {added} boost bar(s) on kept original moments that measure "
                 "quiet next to the voice fader — green bars on the timeline, delete any you "
                 "don't want", level="ok")
    pv.write_audio(os.path.join(d, "voice_raw.wav"), canvas[:, :N])
    meta.update({"mode": mode, "chunks": chunks, "preserves": preserves,
                 "characters": [c["identifier"] for c in chars]})
    _write_json(os.path.join(d, "meta.json"), meta)
    return remix(clip_path, settings, emit)


