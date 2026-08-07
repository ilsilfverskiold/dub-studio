"""The master reel: the proven stitcher with one flat, measured delivery gain."""

import json
import os
import re

import numpy as np

import providers as pv

from . import store


def _dims(path):
    """Video dimensions, parsed from ffmpeg's stream info (even-rounded for encoders)."""
    import re
    r = pv.ff(["-i", path])
    m = re.search(r"Video:.* (\d{2,5})x(\d{2,5})", r.stderr)
    if not m:
        return 720, 1280
    w, h = int(m.group(1)) // 2 * 2, int(m.group(2)) // 2 * 2
    return w, h


def make_reel(finals, settings, emit):
    """Port of the proven stitcher (pipeline_clean/make_reel.py) with ONE deliberate deviation:
    delivery loudness is a MEASURED STATIC gain, not a dynamic loudnorm. This follows
    SOUND_NOTES ("normalize LAST, two-pass ... doesn't pump/breathe the way single-pass
    streaming normalization can") — the old single-pass here measured 20.7 dB of difference
    in how it treated two clips.

    Same trim (0.08s tail), same normalize (fps=24, scale=540:960, 48 kHz), same 300ms xfade +
    acrossfade chain, same encoder settings. The original's single-pass loudnorm rides gain
    moment-to-moment — measured in the field treating two clips 20.7 dB apart, which un-leveled
    the exactly-leveled per-clip dialogue. Now: stitch, measure integrated LUFS + true peak once,
    apply one flat gain to the whole program (capped so TP stays under -1.5). Every clip moves
    by the same amount, so clip-to-clip voice loudness in the master is EXACTLY the per-clip mix."""
    if not finals:
        return None
    out = os.path.join(store.OUTPUT, "reel.mp4")
    XFADE, TRIM_TAIL = 0.30, 0.08
    REEL_LUFS = getattr(settings, "reel_lufs", -18.0)   # master-mix loudness target (master-only knob)
    durs = [(pv.media_duration(f) or 8.0) - TRIM_TAIL for f in finals]
    inputs = []
    for f in finals:
        inputs += ["-i", f]
    parts = []
    for i, d in enumerate(durs):
        parts.append(f"[{i}:v]trim=0:{d:.3f},setpts=PTS-STARTPTS,fps=24,"
                     f"scale=540:960,setsar=1,format=yuv420p[v{i}]")
        parts.append(f"[{i}:a]atrim=0:{d:.3f},asetpts=PTS-STARTPTS,"
                     f"aformat=sample_rates=48000:channel_layouts=stereo[a{i}]")
    vlab, alab = "v0", "a0"
    if len(finals) > 1:
        run_len = durs[0]
        for i in range(1, len(finals)):
            off = run_len - XFADE
            parts.append(f"[{vlab}][v{i}]xfade=transition=fade:duration={XFADE}:offset={off:.3f}[vx{i}]")
            parts.append(f"[{alab}][a{i}]acrossfade=d={XFADE}[ax{i}]")
            vlab, alab = f"vx{i}", f"ax{i}"
            run_len += durs[i] - XFADE
    tmp = os.path.join(store.OUTPUT, "reel_stitch.mp4")
    r = pv.ff(["-y", *inputs, "-filter_complex", ";".join(parts), "-map", f"[{vlab}]",
               "-map", f"[{alab}]", "-c:v", "libx264", "-preset", "medium",
               "-pix_fmt", "yuv420p", "-c:a", "aac", tmp])
    if r.returncode:
        raise RuntimeError(f"reel failed: {r.stderr[-400:]}")
    # measure once, correct once — flat. (loudnorm here is only the METER; nothing is normalized.)
    gain, meas_note = 0.0, "measurement failed — reel delivered at its mixed loudness"
    meas = pv.ff(["-i", tmp, "-af", "loudnorm=print_format=json", "-f", "null", "-"])
    m = re.search(r'\{[^{}]*"input_i"[^{}]*\}', meas.stderr, re.S)
    if m:
        try:
            j = json.loads(m.group(0))
            input_i, input_tp = float(j["input_i"]), float(j["input_tp"])
            if np.isfinite(input_i) and np.isfinite(input_tp):
                gain = REEL_LUFS - input_i
                capped = gain > (-1.5 - input_tp)
                gain = min(gain, -1.5 - input_tp)
                meas_note = (f"measured {input_i:.1f} LUFS -> one flat {gain:+.1f} dB to hit "
                             f"{REEL_LUFS:.0f}" + (" (capped by true peak)" if capped else ""))
        except (ValueError, KeyError):
            pass
    r = pv.ff(["-y", "-i", tmp, "-map", "0:V:0", "-map", "0:a:0", "-c:v", "copy",
               "-af", f"volume={gain:.2f}dB", "-c:a", "aac", out])
    if r.returncode:
        raise RuntimeError(f"reel gain failed: {r.stderr[-400:]}")
    os.remove(tmp)
    emit("reel", f"stitched {len(finals)} clips: 300ms crossfades · {meas_note} — the gain is "
         "FLAT across the whole program, so voice loudness stays exactly as the clips mixed it",
         level="ok",
         detail="a dynamic loudnorm here once treated two clips 20.7 dB apart — measured, so it's gone")
    return out
