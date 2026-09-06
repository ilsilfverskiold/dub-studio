"""Mix (free, re-runnable): the three layers, visible bars, measured levels."""

import json
import os
import shutil
import time

import numpy as np

import audio_engine as ae
import providers as pv
from audio_engine import SR

from . import store
from .overrides import _keep_off_windows
from .store import MIX_LAW, _read_json, _write_json, clip_state_dir
from .takes import clear_star, load_takes, takes_dir


def remix(clip_path, settings, emit):
    """Everything after conversion — dialogue level, tone, bed balance, duck — free to re-run."""
    d = clip_state_dir(clip_path)
    meta = json.load(open(os.path.join(d, "meta.json")))
    dur = meta["duration"]
    N = int(dur * SR)
    final = os.path.join(store.OUTPUT, os.path.splitext(os.path.basename(clip_path))[0] + "_final.mp4")

    if meta["mode"] == "no_voice":
        shutil.copyfile(clip_path, final)
        emit("mix", "original audio kept as-is", level="ok")
        return final

    # a PINNED take with an archived voice track means: use THAT voice performance, and
    # re-mix + clean everything else with the CURRENT rules (user law). Old stemless takes
    # can't do this — starring those still plays their flattened file untouched.
    _tk = load_takes(clip_path)
    pinned = next((x for x in _tk.get("takes", []) if x["id"] == _tk.get("starred")
                   and x.get("voice_file")
                   and os.path.exists(os.path.join(takes_dir(clip_path), x["voice_file"]))), None)
    if pinned:
        raw = pv.read_audio(os.path.join(takes_dir(clip_path), pinned["voice_file"]))
        chunk_list = [list(c) for c in (pinned.get("chunks") or meta.get("chunks", []))]
        emit("mix", "pinned take's VOICE in use — mixed and cleaned with the current rules",
             level="ok")
    else:
        raw = pv.read_audio(os.path.join(d, "voice_raw.wav"))
        chunk_list = list(meta.get("chunks", []))
    voice = np.zeros((2, N), dtype=np.float32)
    n = min(N, raw.shape[1])
    voice[:, :n] = raw[:, :n]
    ed = _read_json(os.path.join(d, "edits.json")) or {}
    voice_mutes = ed.get("voice_mutes", [])
    for a, b in voice_mutes:      # user CUT the STS voice here — the original sound takes over below
        voice[:, int(a * SR):min(N, int(b * SR))] = 0.0
    if voice_mutes:
        emit("mix", f"{len(voice_mutes)} STS cut(s): the new voice is out and the ORIGINAL voice "
             "returns (from the separation) — the background stays leveled underneath; draw a "
             "green boost region if the original should take the front", level="ok",
             detail=" · ".join(f"{a:.2f}-{b:.2f}s" for a, b in voice_mutes))
    # regions whose CONVERT field is off play the ORIGINAL sound. Flipping the field after a
    # conversion is free — the rendered voice is silenced here, nothing re-bills, and flipping
    # it back is free too (the conversion is still on disk).
    keep_windows = [tuple(w) for w in _keep_off_windows(clip_path)]
    for a, b in keep_windows:
        voice[:, int(a * SR):min(N, int(b * SR))] = 0.0
    if keep_windows:
        emit("mix", f"{len(keep_windows)} region(s) set to NOT convert — the original sound "
             "plays there (free either way: flip Convert back anytime, the conversion is kept)",
             level="ok", detail=" · ".join(f"{a:.2f}-{b:.2f}s" for a, b in keep_windows))
    # TTS takes are TIMELINE WINDOWS the user placed and sized: inside each one the STS voice
    # is out and the generated line (spoken from their text) is in — same chain, same leveling
    # as every other piece of dialogue.
    tts_takes = ed.get("tts_takes", []) or []
    extra_chunks = []
    # tonal reference for TTS boom-matching: this clip's converted (STS) dialogue, measured from
    # the RAW conversion — deliberately before user mutes, because a fully-muted STS voice still
    # defines the tonal lane the TTS must sit in. No STS at all -> a conservative constant from
    # field measurements of STS takes (they sit around -10 to -25 dB low-end share).
    sts_ref = None
    if tts_takes:
        if chunk_list:
            segs = [raw[:, int(a * SR):min(raw.shape[1], int(b * SR))] for a, b in chunk_list]
            segs = [s for s in segs if s.shape[1] > SR // 10 and float(np.abs(s).max()) > 1e-6]
            if segs:
                sts_ref = ae.low_share_db(np.concatenate(segs, axis=1))
        if sts_ref is None:
            sts_ref = -12.0
    for i, tk in enumerate(tts_takes):
        place = float(tk.get("place_at", tk["start"]))
        # a stale speech-start far outside the bar means the bar was moved without it (old
        # data): the audio belongs WHERE THE BAR IS — snap to the bar, never to a ghost point
        if not (float(tk["start"]) - 1.0 <= place <= float(tk["end"])):
            place = float(tk["start"])
        s0 = int(place * SR)
        e0 = min(N, int(float(tk["end"]) * SR))
        s0 = min(s0, max(0, e0 - 1))
        p = os.path.join(d, "tts", tk.get("file", "") or "")
        if not (tk.get("file") and os.path.exists(p)):
            emit("retake", f"TTS window {tk['start']:.2f}-{tk['end']:.2f}s has no generated take yet — "
                 "select it on the timeline and Generate", level="warn")
            continue
        wav = pv.read_audio(p)
        if sts_ref is not None:
            # TTS can synthesize far boomier than the STS dialogue around it — cut the MEASURED
            # excess below 250 Hz so both voices enter the shared chain equally clean. The cached
            # take on disk is never touched; this happens fresh at every mix.
            wav, boom_cut = ae.match_low_end(wav, sts_ref)
            if boom_cut:
                emit("mix", f"TTS take at {tk['start']:.2f}s arrived {boom_cut:.1f} dB boomier than "
                     f"the converted dialogue — low end cut to match before the voice chain",
                     level="metric",
                     detail=f"measured low-end share vs this clip's STS dialogue · shelf below 250 Hz · "
                            f"never a guess, never a boost")
        w0 = int(float(tk["start"]) * SR)
        voice[:, w0:e0] = 0.0                        # the STS leaves the whole window
        # THE BAR IS THE TAKE (user law): audio never plays past the bar's edge. Generate
        # sizes the bar to the audio; a hand-shortened bar audibly trims the line (the edge
        # gets the same short fade as every chunk edge below). Before this, a long take ran
        # invisibly past its bar and overwrote the added voice on later regions.
        L = min(wav.shape[1], N - s0, max(0, e0 - s0))
        voice[:, s0:s0 + L] = wav[:, :L]
        extra_chunks.append([round(s0 / SR, 3), round((s0 + L) / SR, 3)])
        cut = (wav.shape[1] - L) / SR
        emit("retake", f"TTS in place at {tk['start']:.2f}s — “{tk.get('text','')[:60]}”" +
             (f" — the bar cuts the take {cut:.2f}s short; drag its edge out to hear the rest"
              if cut > 0.05 else ""),
             level="warn" if cut > 0.05 else "ok",
             detail="spoken from your text in the character's voice · same chain and leveling as all dialogue")
    # constant dialogue level per placed chunk (STS levels are arbitrary per call), plus a short
    # fade at each chunk edge — hard edges click audibly at every boundary. An STS chunk that
    # CONTAINS a TTS window is measured on its STS samples only — the inserted take must not
    # drag the surrounding dialogue off target (the take gets its own exact norm right after,
    # extra_chunks come last). This is what makes STS and TTS land at the SAME loudness.
    fade = int(0.008 * SR)
    tts_mask = np.zeros(voice.shape[1], dtype=bool)
    for a, b in extra_chunks:
        tts_mask[int(a * SR):min(N, int(b * SR))] = True
    for a, b in list(chunk_list) + extra_chunks:
        s0, e0 = int(a * SR), min(N, int(b * SR))
        m = ~tts_mask[s0:e0]
        voice[:, s0:e0] = ae.dialogue_norm(voice[:, s0:e0],
                                           measure=None if m.all() else m)
        if e0 - s0 > 2 * fade:
            ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            voice[:, s0:s0 + fade] *= ramp
            voice[:, e0 - fade:e0] *= ramp[::-1]
    base = ae.finalize_voice(ae.voice_chain(settings)(voice, SR), settings)
    # THE LEVEL-SET — genuinely last, measured on the speech itself: the fader number IS the
    # measured loudness of the rendered voice (the old in-chain level drifted ~6 dB from the
    # knob). Zeroed windows contribute nothing (active_rms ignores silence).
    _spans = [base[:, int(a * SR):min(N, int(b * SR))] for a, b in list(chunk_list) + extra_chunks]
    _spans = [s_ for s_ in _spans if s_.shape[1]]
    voice_meas_db = None
    if _spans:
        a_meas = ae.active_rms(np.concatenate(_spans, axis=1))
        if a_meas > 1e-8:
            # the canvas ran the chain at CHAIN_DB; the fader lands HERE as one flat gain.
            # Clamp spans the whole fader range (-50..-5 around -16). The meter is pre-scale
            # measurement + applied gain — exact, and immune to the -45 active floor slicing
            # into speech at very low fader settings.
            g = float(np.clip((10 ** (settings.dialog_db / 20.0)) / a_meas,
                              10 ** (-40 / 20.0), 10 ** (40 / 20.0)))
            base = base * g
            voice_meas_db = 20.0 * np.log10(a_meas * g + 1e-12)
    emit("mix", f"voice chain applied: HPF {settings.hpf_hz:.0f} Hz · boom {settings.bass_shelf_db:+.1f} dB · "
         f"notch {settings.notch_db:+.1f} dB @ {settings.notch_hz:.0f} Hz · presence {settings.presence_db:+.1f} · "
         f"air {settings.air_db:+.1f} · de-ess {settings.deess_db:.1f} · comp {settings.comp_ratio:.1f}:1 · "
         f"room {settings.room_wet:.2f}",
         level="metric",
         detail="the values THIS mix actually rendered with — also recorded in the clip's take history")

    # heal: old meta.json may hold separation paths from before the caches moved into the
    # project dir. Content-hashed, so re-resolving is a cache hit (or a free local re-separation).
    if not (os.path.exists(meta["instrumental"]) and os.path.exists(meta["vocals"])) or \
            f"_{pv.sep_model_key()}_" not in os.path.basename(meta["instrumental"]):
        # missing stems, OR the configured separation model changed — re-separate (local, free)
        inst, voc = pv.separate(clip_path, emit=emit)
        meta["instrumental"], meta["vocals"] = inst, voc
        _write_json(os.path.join(d, "meta.json"), meta)
    bed = pv.read_audio(meta["instrumental"])
    orig_voice = pv.read_audio(meta["vocals"])
    bed = np.pad(bed, ((0, 0), (0, max(0, N - bed.shape[1]))))[:, :N]
    orig_voice = np.pad(orig_voice, ((0, 0), (0, max(0, N - orig_voice.shape[1]))))[:, :N]

    bed = ae.bed_rumble(bed, settings)
    bed, gdb, bed_capped = ae.bed_level(bed, settings)
    bed_meas_db = 20.0 * np.log10(ae.active_rms(bed) + 1e-12)
    cuts = [tuple(w) for w in voice_mutes]
    excl = []
    for w in [tuple(p) for p in meta.get("preserves", [])] + [tuple(k) for k in keep_windows] + cuts:
        if w not in excl:
            excl.append(w)
    duck_regions = ed.get("duck_regions", [])
    if not bool(ed.get("duck_owned")):
        # auto-ducking IS automatic — but always ON the timeline (user law: "if it's on the
        # timeline, that's fine — we can work with it, remove it"). A clip with no duck map
        # gets bars drawn where a voice speaks, once; from then on they're the user's.
        wins = ae.propose_duck_windows(base, orig_voice, exclude_windows=excl, n=N) \
            if settings.duck else []
        duck_regions = [{"start": w[0], "end": w[1]} for w in wins]
        ed["duck_regions"] = duck_regions
        ed["duck_owned"] = True
        _write_json(os.path.join(d, "edits.json"), ed)
        if duck_regions:
            emit("mix", f"drew {len(duck_regions)} duck bar(s) where a voice speaks — on the "
                 "timeline, yours: move, resize or delete any of them", level="ok",
                 detail=" · ".join(f"{r['start']:.2f}-{r['end']:.2f}s" for r in duck_regions))
    emit("mix", f"background leveled: fader {settings.bed_db:+.0f} dB -> measured "
         f"{bed_meas_db:.1f} dB ({gdb:+.1f} dB applied)" +
         (" — the +24 dB safety limit held it below the fader (very quiet source track)"
          if bed_capped else " — the fader is TRUE, both directions"),
         level="metric")

    # THE ORIGINAL VOICE IS ITS OWN LAYER (user architecture): the extracted original voice
    # plays as one continuous layer, separate from the background. Wherever the NEW voice
    # speaks, the tool DRAWS a visible turn-down region on this layer (default: fully out) —
    # never a hidden gate. The user sees every turn-down, adjusts its amount, or deletes it.
    dips = ed.get("voice_dips", []) or []
    dmarks = {(round(float(m[0]), 2), round(float(m[1]), 2)) for m in ed.get("proposed_dips", [])}
    # EFFECTIVE new-voice spans: chunks and TTS placements MINUS the user's mutes — an auto
    # turn-down exists only because a new voice actually plays on top there.
    def _minus_mutes(a, b):
        spans = [(a, b)]
        for m0, m1 in voice_mutes:
            spans = [s_ for seg_ in spans
                     for s_ in (((seg_[0], min(seg_[1], m0)), (max(seg_[0], m1), seg_[1])))
                     if s_[1] - s_[0] > 0.05]
        return spans
    # the turn-down must cover the ORIGINAL voice's OWN burst — attack to tail — not just the
    # new-voice rectangle. A tail ringing past the window played right after the replacement
    # and sounded like an echo on every line (the bug the user caught).
    ov_runs = ae._state_runs_seconds(ae.speech_state_gain(orig_voice), N)
    eff = []
    for a0, b0 in list(chunk_list) + extra_chunks:
        a1, b1 = round(float(a0), 2), round(float(b0), 2)
        for ra, rb in ov_runs:
            if ra < b1 and rb > a1:            # original burst overlapping the line
                a1, b1 = min(a1, round(ra - 0.15, 2)), max(b1, round(rb + 0.25, 2))
        eff += _minus_mutes(max(0.0, a1), b1)
    # merge overlapping spans so one line = one bar
    eff = sorted((round(a, 2), round(b, 2)) for a, b in eff)
    merged = []
    for a, b in eff:
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    eff = merged
    # auto bars REGENERATE from the spans above: untouched ones (db 60, auto-marked) are
    # replaced; user-modified or hand-drawn bars are never touched
    prev_marks = set(dmarks)
    dips = [dd for dd in dips
            if not ((round(float(dd["start"]), 2), round(float(dd["end"]), 2)) in dmarks
                    and float(dd.get("db", 60.0)) == 60.0)]
    dmarks = set()
    drawn = 0
    for a, b in eff:
        dmarks.add((a, b))
        if not any(dd["end"] > a and dd["start"] < b for dd in dips):
            dips.append({"start": a, "end": b, "db": 60.0})
            drawn += 1
    drawn = drawn if dmarks != prev_marks else 0   # only announce when the map actually changed
    # SELF-CLEANING: an auto-drawn, untouched turn-down whose new voice is GONE (the user muted
    # or removed it) removes itself — the original plays again there. User-modified or
    # user-created dips are never touched.
    pruned = []
    kept = []
    for dd in dips:
        key = (round(float(dd["start"]), 2), round(float(dd["end"]), 2))
        is_auto = key in dmarks and float(dd.get("db", 60.0)) == 60.0
        has_voice = any(e1 > dd["start"] and e0 < dd["end"] for e0, e1 in eff)
        if is_auto and not has_voice:
            pruned.append(dd)
            dmarks.discard(key)        # if the voice returns later, the dip may be re-proposed
        else:
            kept.append(dd)
    dips = kept
    ed["voice_dips"] = dips
    ed["proposed_dips"] = sorted(list(m) for m in dmarks)
    _write_json(os.path.join(d, "edits.json"), ed)
    if drawn:
        emit("mix", f"drew {drawn} turn-down region(s) on the ORIGINAL-voice layer under the "
             "new voice — visible on the timeline; adjust each one's amount or delete it",
             level="ok",
             detail=" · ".join(f"{dd['start']:.2f}-{dd['end']:.2f}s" for dd in dips) or "none")
    if pruned:
        emit("mix", f"removed {len(pruned)} turn-down region(s) whose new voice is gone — the "
             "ORIGINAL voice plays again there", level="ok",
             detail=" · ".join(f"{dd['start']:.2f}-{dd['end']:.2f}s" for dd in pruned))
    orig = orig_voice.copy()
    if dips:
        env_o = np.ones(orig.shape[1], dtype=np.float32)
        rampn = int(0.04 * SR)
        for dd in dips:
            s0, e0 = max(0, int(dd["start"] * SR)), min(orig.shape[1], int(dd["end"] * SR))
            if e0 - s0 <= 0:
                continue
            _vdb = abs(float(dd.get("db", 60.0)))
            cut = 0.0 if _vdb >= 60 else 10 ** (-_vdb / 20.0)    # 60 = remove entirely, true silence
            env_o[s0:e0] = np.minimum(env_o[s0:e0], cut)
            r0 = min(rampn, s0)
            if r0:
                env_o[s0 - r0:s0] = np.minimum(env_o[s0 - r0:s0], np.linspace(1.0, cut, r0))
            r1 = min(rampn, orig.shape[1] - e0)
            if r1:
                env_o[e0:e0 + r1] = np.minimum(env_o[e0:e0 + r1], np.linspace(cut, 1.0, r1))
        orig = orig * env_o[None, :]
    mixed = base + bed + orig

    # USER DUCK BARS ARE LAW: each bar dips everything except the new voice by ITS OWN amount
    # (per-bar, default = the Duck-depth fader), flat, ramped 40 ms. Nothing overrides them.
    if duck_regions:
        bg = mixed - base
        env_d = np.ones(bg.shape[1], dtype=np.float32)
        rampn = int(0.04 * SR)
        for r in duck_regions:
            s0, e0 = max(0, int(r["start"] * SR)), min(bg.shape[1], int(r["end"] * SR))
            if e0 - s0 <= 0:
                continue
            _ddb = abs(float(r.get("db", settings.duck_under_db)))
            cutg = 0.0 if _ddb >= 60 else 10 ** (-_ddb / 20.0)   # 60 = remove entirely, true silence
            env_d[s0:e0] = np.minimum(env_d[s0:e0], cutg)
            r0 = min(rampn, s0)
            if r0:
                env_d[s0 - r0:s0] = np.minimum(env_d[s0 - r0:s0], np.linspace(1.0, cutg, r0))
            r1 = min(rampn, bg.shape[1] - e0)
            if r1:
                env_d[e0:e0 + r1] = np.minimum(env_d[e0:e0 + r1], np.linspace(cutg, 1.0, r1))
        mixed = base + bg * env_d[None, :]
        emit("mix", f"{len(duck_regions)} duck bar(s) — each dips by its own amount", level="ok",
             detail=" · ".join(f"{r['start']:.2f}-{r['end']:.2f}s -{abs(float(r.get('db', settings.duck_under_db))):.0f} dB"
                               for r in duck_regions) + " · your bars are law — nothing overrides them")

    # BOOST BARS: each lifts everything except the new voice by ITS OWN amount (per-bar dB,
    # default +6), flat, ramped 40 ms. The user decides the amount per bar.
    boost_regions = ed.get("boost_regions", [])
    if boost_regions:
        bg = mixed - base
        env = np.ones(bg.shape[1], dtype=np.float32)
        rampn = int(0.04 * SR)
        for r in boost_regions:
            s0, e0 = int(r["start"] * SR), min(bg.shape[1], int(r["end"] * SR))
            if e0 - s0 <= 0:
                continue
            g = 10 ** (abs(float(r.get("db", 6.0))) / 20.0)
            env[s0:e0] = np.maximum(env[s0:e0], g)
            r0 = min(rampn, s0)
            if r0:
                env[s0 - r0:s0] = np.maximum(env[s0 - r0:s0], np.linspace(1.0, g, r0))
            r1 = min(rampn, bg.shape[1] - e0)
            if r1:
                env[e0:e0 + r1] = np.maximum(env[e0:e0 + r1], np.linspace(g, 1.0, r1))
        mixed = base + bg * env[None, :]
        emit("mix", f"{len(boost_regions)} boost bar(s) — each lifts by its own amount", level="ok",
             detail=" · ".join(f"{r['start']:.2f}-{r['end']:.2f}s +{abs(float(r.get('db', 6.0))):.0f} dB"
                               for r in boost_regions))

    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 0.98:
        k = 0.98 / peak
        mixed *= k
        base = base * k     # keep the solo stems honest: bed = mixed - base must hold exactly
    mix_wav = os.path.join(d, "mix.wav")
    pv.write_audio(mix_wav, mixed)
    # solo stems, so the user can HEAR what each layer contributes (served by /media/solo/)
    pv.write_audio(os.path.join(d, "solo_voice.wav"), base)
    pv.write_audio(os.path.join(d, "solo_bed.wav"), mixed - base)
    r = pv.ff(["-y", "-i", clip_path, "-i", mix_wav, "-filter_complex",
               f"[1:a]aformat=sample_rates={SR}:channel_layouts=stereo,apad,atrim=0:{dur:.3f}[a]",
               "-map", "0:V:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
               "-shortest", final])
    if r.returncode:
        raise RuntimeError(f"mux failed: {r.stderr[-400:]}")
    # THE PROOF LINE — every render states its measured levels next to the fader numbers
    if voice_meas_db is not None:
        emit("mix", f"LEVELS measured: voice {voice_meas_db:.1f} dB (fader {settings.dialog_db:+.0f}) · "
             f"background {bed_meas_db:.1f} dB (fader {settings.bed_db:+.0f}) — both faders are TRUE",
             level="metric")
    # record exactly what this mix was rendered with — drives the mix-stale flag
    meta["mix_law"] = MIX_LAW              # which LAW rendered this — old-law mixes self-heal
    meta["rendered_with"] = settings.to_dict()
    meta["rendered_at"] = time.time()
    meta["duck_windows"] = sorted([[r["start"], r["end"]] for r in duck_regions])
    meta["rendered_duck"] = duck_regions                  # the visible duck bars this mix used
    meta["rendered_duck_owned"] = True                    # bars are always materialized now
    meta["rendered_keep_off"] = [[a, b] for a, b in keep_windows]   # Convert-off regions used
    meta["rendered_boost"] = ed.get("boost_regions", [])
    meta["rendered_dips"] = ed.get("voice_dips", [])      # original-voice turn-downs this mix used
    meta["rendered_tts"] = tts_takes
    meta["rendered_voice_mutes"] = voice_mutes            # user voice mutes this mix used
    _write_json(os.path.join(d, "meta.json"), meta)
    if not pinned:
        clear_star(clip_path)             # a fresh mix supersedes an ordinary star; a PINNED
                                          # voice survives renders — the render implements it
    emit("mix", f"-> {os.path.basename(final)}", level="ok")
    return final


