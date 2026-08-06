"""The Dub Studio pipeline.

Structure (deliberate, learned the hard way — see README):
  1. DIARIZE (local, free): how many voices speak, and when.
  2. IDENTIFY (Gemini, pennies): who the character is, so the same character keeps the same voice
     across clips. With 2+ voices it also labels each region (words / wordless) — with ONE voice it
     does NOT get to make word-level decisions at all.
  3. CONVERT (ElevenLabs, paid, cached):
       one voice  -> isolate the voice, send the WHOLE stem to STS in ONE call, then align the result
                     back to the original timeline ourselves (STS shuffles silences; timing is our job).
       two+ voices -> split per region per speaker; never send a wordless slice (bare laughter) alone —
                     it hallucinates words; wordless regions are preserved from the original.
  4. MIX (local, free, re-runnable): separated background bed + converted dialogue, with every
     audio-engineering decision exposed as a setting.
"""

import json
import os
import re
import shutil
import sys
import threading
import time

import numpy as np

import providers as pv
import audio_engine as ae
from audio_engine import SR, Settings

# The mix LAW version. Bump this whenever the leveling/mix rules change — every clip rendered
# under an older law flags itself mix-stale IN EVERY PROJECT and re-renders free, automatically.
# Correctness is never a manual, per-clip chore.
# v4 (2026-08-06, THE LAW — built with the user, plan let-s-try-to-figure):
#   fader numbers are ABSOLUTE and TRUE (measured) · background leveled as ONE whole per clip,
#   never doubled · original sound plays AS RECORDED · engine opinions only ever appear as
#   VISIBLE bars the user can delete · no voice on voice inside a duck bar · flat master gain.
# v5 (2026-08-06): boost bars are proposed on EVERY original-sound window including voice cuts
#   (user spec) · auto-duck draws its bars ON the timeline (visible, user-owned).
# v6 (2026-08-06): a boost bar RAISES TO the voice fader's level, measured per bar — never a
#   blind flat gain, never past the voice, never down.
# v7 (2026-08-06): the background is ONE thing — superseded by v8.
# v8 (2026-08-06, user architecture): THREE SEPARATE LAYERS — new voice · original voice (the
#   extracted stem, its own continuous layer) · background (leveled DOWN-only to its fader,
#   never inflated). The tool's turn-downs of the original voice under new-voice lines are
#   VISIBLE regions (voice_dips) the user can adjust or delete. Duck and boost bars each carry
#   their OWN adjustable amount.
# v9 (2026-08-06): auto turn-downs follow the EFFECTIVE new voice (after mutes) and remove
#   themselves when their voice is gone · a TTS take's speech-start snaps to its bar.
# v10 (2026-08-06): the background fader is TRUE both directions (+24 dB amplification limit,
#   logged when it engages) — the dead up-knob forced manual per-clip voice dragging.
# v11 (2026-08-06): auto turn-downs cover the ORIGINAL voice's full burst (attack to tail)
#   around each line — narrow rectangles let the original's ring play beside the replacement
#   and every voice sounded echoey.
MIX_LAW = 11

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")      # per-project since v2 — server calls set_project_dirs()
OUTPUT = os.path.join(HERE, "output")


def set_project_dirs(state_dir, output_dir):
    """Point all pipeline state at the active project. No import-time makedirs on the legacy
    paths — empty legacy dirs resurrecting after migration would confuse the migration guard."""
    global STATE, OUTPUT
    STATE, OUTPUT = state_dir, output_dir
    for d in (STATE, OUTPUT):
        os.makedirs(d, exist_ok=True)

ARCHES = ["animated boy", "animated girl", "animated character", "young boy", "young girl",
          "young man", "young woman", "middle-aged man", "old man", "old woman", "deep-voiced man"]


# ---------------------------------------------------------------- cast (voice consistency)

def _norm_id(s):
    """Gemini returns identifiers as 'name' or '@name' unpredictably; one canonical form everywhere."""
    return (s or "").strip().lstrip("@").strip()


def load_cast():
    p = os.path.join(STATE, "cast.json")
    cast = _read_json(p, []) or []
    changed = False
    for c in cast:
        n = _norm_id(c.get("identifier", ""))
        if n != c.get("identifier"):
            c["identifier"] = n
            changed = True
    # De-dupe: one row per identifier, keep-LAST. Duplicates poison the tool because
    # writes hit the first match while status/convert resolve last-wins — the exact
    # "voice change never shows" bug. Keep-LAST preserves what the UI is showing.
    seen = {}
    for c in cast:
        seen[c["identifier"]] = c
    if len(seen) != len(cast):
        cast = list(seen.values())
        changed = True
    if changed:
        _write_json(p, cast)
    return cast


def save_cast(cast):
    _write_json(os.path.join(STATE, "cast.json"), cast)


def reset_cast():
    p = os.path.join(STATE, "cast.json")
    if os.path.exists(p):
        os.remove(p)


# ---------------------------------------------------------------- analyze (free)

def clip_state_dir(clip_path):
    d = os.path.join(STATE, pv.file_hash(clip_path))
    os.makedirs(d, exist_ok=True)
    return d


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




# ---------------------------------------------------------------- staged state (user-editable)

def _read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        return json.load(open(path))
    except (json.JSONDecodeError, OSError):
        # a corrupt state file must never take the whole app down. Preserve it for inspection
        # (nothing is ever lost silently), then behave as if it were absent — the next save
        # rebuilds it. Corruption seen in the field: two unlocked open("w") writers interleaving.
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        print(f"corrupt state file preserved as {path}.corrupt", file=sys.stderr)
        return default


def _write_json(path, obj):
    """Atomic write for ALL state files: tmp + os.replace, so a torn or interleaved write can
    never leave half-JSON on disk (the bug that corrupted an edits.json under rapid timeline
    saves). Thread-ident in the tmp name keeps concurrent writers off each other's tmp file."""
    tmp = f"{path}.tmp{threading.get_ident()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def clip_stage(clip_path):
    """uploaded -> detected -> cast -> converted (final playable)."""
    d = clip_state_dir(clip_path)
    if _read_json(os.path.join(d, "meta.json")):
        return "converted"
    if _read_json(os.path.join(d, "who.json")):
        return "cast"
    if _read_json(os.path.join(d, "analysis.json")):
        return "detected"
    return "uploaded"


def effective_detection(clip_path):
    """Analysis overlaid with the user's edits — the pipeline always uses THIS, never raw analysis."""
    d = clip_state_dir(clip_path)
    ana = _read_json(os.path.join(d, "analysis.json")) or {}
    ed = _read_json(os.path.join(d, "edits.json")) or {}
    regions = ed.get("regions", ana.get("regions", []))
    voices = ed.get("voices", ana.get("voices", 0))
    return regions, voices, ana


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


def save_assign_edits(clip_path, assigns):
    """The user says who speaks where — and, per region, whether it CONVERTS at all (the
    visible Convert field). Flipping Convert after a conversion is a FREE mix change: the
    rendered voice is silenced there at mix time and the original plays; nothing re-bills."""
    d = clip_state_dir(clip_path)
    who = _read_json(os.path.join(d, "who.json"))
    if not who:
        raise RuntimeError("cast this clip first (or add a character)")
    # any character from the PROJECT cast may speak in any clip — not just this clip's chars
    valid = {c["identifier"] for c in who.get("chars", [])} | \
            {_norm_id(c["identifier"]) for c in load_cast()}
    old = who.get("assigns", [])
    out = []
    for a in assigns:
        kind = a.get("kind", "speech")
        if kind not in ("speech", "singing", "human_nonword", "animal_vocal", "non_speech"):
            kind = "speech"
        ch = _norm_id(a.get("character", ""))
        row = {"character": ch if ch in valid else (a.get("character") or ""), "kind": kind}
        if "convert" in a:
            row["convert"] = bool(a["convert"])
        out.append(row)
    who["assigns"] = out
    _write_json(os.path.join(d, "who.json"), who)
    # who/what changed -> the paid conversion is stale. Only Convert flags changed -> the
    # conversion stands; the next free mix applies the flip.
    core = lambda rows: [(r.get("character"), r.get("kind")) for r in rows]
    if core(out) != core(old):
        mp = os.path.join(d, "meta.json")
        if os.path.exists(mp):
            os.remove(mp)


def char_edit(action, identifier, new_identifier=None, clip_path=None, updates=None):
    """User-controlled cast: add a character yourself, or rename one everywhere (registry + clips)."""
    identifier = _norm_id(identifier)
    cast = load_cast()
    if action == "add":
        if any(_norm_id(c["identifier"]) == identifier for c in cast):
            raise RuntimeError(f"@{identifier} already exists")
        vid, name, vg = pv.pick_voice("not-important", "not-important",
                                      exclude={c.get("voice_id") for c in cast})
        cast.append({"identifier": identifier, "description": "animated character",
                     "gender": "not-important", "age": "not-important", "language": "unknown",
                     "role": "supporting", "voice_id": vid, "voice_gender": vg})
        save_cast(cast)
        if clip_path:
            d = clip_state_dir(clip_path)
            who = _read_json(os.path.join(d, "who.json")) or {"chars": [], "assigns": [], "voice_of": {}}
            who["chars"].append({"identifier": identifier, "description": "animated character",
                                 "gender": "not-important", "age": "not-important",
                                 "language": "unknown", "role": "supporting"})
            who["voice_of"][identifier] = vid
            # seed assignments for every region so the who-speaks-where dropdowns appear immediately —
            # manual casting must work end-to-end without ever touching Gemini
            regions, _, _ = effective_detection(clip_path)
            assigns = who.get("assigns", [])[:len(regions)]
            assigns += [{"character": identifier, "kind": "speech"}] * (len(regions) - len(assigns))
            who["assigns"] = assigns
            _write_json(os.path.join(d, "who.json"), who)
        return name
    if action == "update":
        fields = {k: v for k, v in (updates or {}).items()
                  if k in ("description", "gender", "age", "language", "role") and v}
        if not fields:
            raise RuntimeError("nothing to update")
        for c in cast:
            if _norm_id(c["identifier"]) == identifier:
                c.update(fields)
        save_cast(cast)
        for dname in os.listdir(STATE):
            wp = os.path.join(STATE, dname, "who.json")
            who = _read_json(wp)
            if not who:
                continue
            changed = False
            for ch in who.get("chars", []):
                if _norm_id(ch.get("identifier", "")) == identifier:
                    ch.update(fields)
                    changed = True
            if changed:
                _write_json(wp, who)
        return " · ".join(f"{k}: {v}" for k, v in fields.items())
    if action == "rename":
        new_id = _norm_id(new_identifier or "")
        if not new_id:
            raise RuntimeError("empty name")
        # Collision = merge: the existing target row keeps its voice; the renamed row is dropped.
        # A plain rename into an existing id would create duplicate rows, and duplicates are
        # exactly what broke voice changes (write-first vs read-last).
        if any(_norm_id(c["identifier"]) == new_id for c in cast):
            cast = [c for c in cast if _norm_id(c["identifier"]) != identifier]
        else:
            for c in cast:
                if _norm_id(c["identifier"]) == identifier:
                    c["identifier"] = new_id
        save_cast(cast)
        for dname in os.listdir(STATE):
            wp = os.path.join(STATE, dname, "who.json")
            who = _read_json(wp)
            if not who:
                continue
            changed = False
            for ch in who.get("chars", []):
                if _norm_id(ch.get("identifier", "")) == identifier:
                    ch["identifier"] = new_id
                    changed = True
            for a in who.get("assigns", []):
                if _norm_id(a.get("character", "")) == identifier:
                    a["character"] = new_id
                    changed = True
            if identifier in who.get("voice_of", {}):
                who["voice_of"][new_id] = who["voice_of"].pop(identifier)
                changed = True
            if changed:
                _write_json(wp, who)
        return new_id
    raise RuntimeError("unknown action")


def save_kind_edits(clip_path, kinds):
    """Override region labels (speech / laughter / animal / SFX). Conversion becomes stale."""
    d = clip_state_dir(clip_path)
    who = _read_json(os.path.join(d, "who.json"))
    if not who:
        raise RuntimeError("identify the cast first")
    for idx, kind in (kinds or {}).items():
        i = int(idx)
        if 0 <= i < len(who["assigns"]) and kind in ("speech", "singing", "human_nonword", "animal_vocal", "non_speech"):
            who["assigns"][i]["kind"] = kind
    _write_json(os.path.join(d, "who.json"), who)
    mp = os.path.join(d, "meta.json")
    if os.path.exists(mp):
        os.remove(mp)


def set_cast_voice(identifier, voice_id):
    """User swaps a character's voice. Updates EVERY matching row (belt and braces on top of
    load_cast's de-dupe) and returns the voice_id actually persisted — re-read from disk — so the
    caller logs truth, not intent. Returns None if the character is unknown everywhere.
    If the registry lost the character (e.g. an old reset), it is rebuilt from the clip's own
    cast data instead of failing."""
    ident = _norm_id(identifier)
    cast = load_cast()
    hit = False
    for c in cast:
        if _norm_id(c["identifier"]) == ident:
            c["voice_id"] = voice_id.strip()
            hit = True
    if hit:
        save_cast(cast)
    else:
        for dname in os.listdir(STATE):
            who = _read_json(os.path.join(STATE, dname, "who.json"))
            if not who:
                continue
            for ch in who.get("chars", []):
                if _norm_id(ch.get("identifier", "")) == ident:
                    cast.append({"identifier": ident,
                                 **{k: ch.get(k) for k in ("description", "gender", "age", "language", "role")},
                                 "voice_id": voice_id.strip(), "voice_gender": ch.get("gender", "")})
                    save_cast(cast)
                    hit = True
                    break
            if hit:
                break
    if not hit:
        return None
    for c in load_cast():
        if _norm_id(c["identifier"]) == ident:
            return c.get("voice_id")
    return None


def identify_clip(clip_path, emit):
    """Stage 3: Gemini identity + voice lock, persisted for review/edit. ~2 cents."""
    regions, n_voices, _ = effective_detection(clip_path)
    if not regions:
        d = clip_state_dir(clip_path)
        _write_json(os.path.join(d, "who.json"), {"chars": [], "assigns": [], "voice_of": {}})
        emit("identify", "no voice regions — nothing to cast", level="ok")
        return
    cast = load_cast()
    emit("cast", "Gemini watches the actual footage once to decide WHO each voice belongs to — "
         "identity only, so the same character keeps the same voice in every clip; "
         "it makes no word-level decisions",
         detail=f"{pv.GEMINI_MODEL} · sees video+audio · knows the cast from earlier clips")
    chars, assigns = identify(clip_path, regions, cast, emit)
    for c in chars:
        c["identifier"] = _norm_id(c.get("identifier", ""))
    for a in assigns:
        if a.get("character"):
            a["character"] = _norm_id(a["character"])
    voice_of = lock_voices(chars, cast, emit) if chars else {}
    save_cast(cast)
    d = clip_state_dir(clip_path)
    _write_json(os.path.join(d, "who.json"),
                {"chars": chars, "assigns": assigns, "voice_of": voice_of})


# ---------------------------------------------------------------- identify (Gemini)

def identify(clip_path, regions, cast, emit):
    known = "\n".join(f"- @{c['identifier']}: {c['description']} ({c['gender']})" for c in cast) or \
            "(none yet — this is the first clip)"
    rstr = "; ".join(f"[{i}] {r['start']:.2f}-{r['end']:.2f}s" for i, r in enumerate(regions))
    prompt = (
        "You are dubbing an ongoing animated series and MUST keep character voices consistent across "
        "clips. Characters already identified in earlier clips:\n" + known + "\n\n"
        "Watch THIS clip. Sound was detected in these regions (seconds), in order: " + rstr + ".\n\n"
        "1) Identify every distinct CHARACTER that uses its VOICE (words or wordless vocal sounds). "
        "Reuse the EXACT @identifier for a known character. Most clips have ONE voice — do not invent a "
        "second speaker for a monologue. Bare animal noises and SFX are NOT voice characters.\n"
        "2) For EACH region (same order, same count) give {character, kind}. kinds: 'speech' = spoken "
        "WORDS (interjection words like wow/whoa/hey/yay count as speech); 'singing' = sung words or "
        "melody; 'human_nonword' = a voice "
        "with NO words (laughter, sigh, gasp, hum, cry); 'animal_vocal' = bare animal noise; "
        "'non_speech' = SFX/ambience/music/movement.\n\n"
        "description: pick ONE archetype from: " + ", ".join(ARCHES) + ". "
        "gender: male/female/not-important. age: young/middle_aged/old/not-important. "
        "language: short code like en/es/fr or unknown. role: 'main' for the recurring protagonist "
        "(one per series, same every clip) else 'supporting'.")
    ch = {"type": "object", "properties": {
        "identifier": {"type": "string"}, "description": {"type": "string", "enum": ARCHES},
        "gender": {"type": "string", "enum": ["male", "female", "not-important"]},
        "age": {"type": "string", "enum": ["young", "middle_aged", "old", "not-important"]},
        "language": {"type": "string"},
        "role": {"type": "string", "enum": ["main", "supporting"]}},
        "required": ["identifier", "description", "gender", "age", "language", "role"]}
    rg = {"type": "object", "properties": {"character": {"type": "string"},
          "kind": {"type": "string", "enum": ["speech", "singing", "human_nonword", "animal_vocal", "non_speech"]}},
          "required": ["character", "kind"]}
    schema = {"type": "object", "properties": {"characters": {"type": "array", "items": ch},
              "regions": {"type": "array", "items": rg}}, "required": ["characters", "regions"]}
    who = pv.gemini(clip_path, prompt, schema)
    chars, assigns = who.get("characters", []), who.get("regions", [])
    if len(assigns) != len(regions):
        raise RuntimeError(f"Gemini returned {len(assigns)} assignments for {len(regions)} regions")
    for c in chars:
        emit("identify", f"@{c['identifier']} = {c['description']} / {c['gender']} / "
             f"{c.get('age')} [{c.get('role')}]",
             detail="these traits drive the voice pick — edit any of them in the Cast panel")
    return chars, assigns


def lock_voices(chars, cast, emit):
    """Exact @id -> stored voice. New main -> reuse the main voice. Otherwise pick from the account.
    All lookups go through _norm_id: a raw-key miss here appended duplicate cast rows, which is
    the root of the 'voice change never shows' bug."""
    byid = {_norm_id(c["identifier"]): c for c in cast}
    main_vid = next((c["voice_id"] for c in cast if c.get("role") == "main" and c.get("voice_id")), None)
    voice_of, used = {}, set()
    for c in chars:
        role = (c.get("role") or "supporting").lower()
        if _norm_id(c["identifier"]) in byid:
            vid = byid[_norm_id(c["identifier"])]["voice_id"]
            note = "reuse (exact id)"
        elif role == "main" and main_vid and main_vid not in used:
            vid = main_vid
            note = "reuse main voice (re-named protagonist)"
        else:
            vid, name, vg = pv.pick_voice(c.get("gender", ""), c.get("age", ""),
                                          language=c.get("language", ""),
                                          exclude=used | {x.get("voice_id") for x in cast})
            cast.append({**{k: c.get(k) for k in ("identifier", "description", "gender", "age",
                                                  "language", "role")},
                         "identifier": _norm_id(c["identifier"]),
                         "voice_id": vid, "voice_gender": vg})
            byid[_norm_id(c["identifier"])] = cast[-1]
            if role == "main" and main_vid is None:
                main_vid = vid
            note = f"NEW -> {name}"
        used.add(vid)
        voice_of[c["identifier"]] = vid
        emit("voices", f"@{c['identifier']} -> {note}",
             detail="picked from your ElevenLabs voice library by gender/age/language · swap it any time")
    return voice_of


# ---------------------------------------------------------------- per-clip mix overrides (v2)

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


# ---------------------------------------------------------------- takes (v2)

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


# ---------------------------------------------------------------- re-detect at a sensitivity (v2, free)

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


# ---------------------------------------------------------------- convert (paid, cached)

def convert_clip(clip_path, emit, settings=None):
    """Stage 4 (paid): isolation + STS using exactly the reviewed regions/voices/kinds/cast."""
    settings = settings or Settings()
    d = clip_state_dir(clip_path)
    # a re-convert supersedes the current take: archive it first (nothing is ever lost silently)
    prev_final = os.path.join(OUTPUT, os.path.splitext(os.path.basename(clip_path))[0] + "_final.mp4")
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


# ---------------------------------------------------------------- mix (free, re-runnable)

def remix(clip_path, settings, emit):
    """Everything after conversion — dialogue level, tone, bed balance, duck — free to re-run."""
    d = clip_state_dir(clip_path)
    meta = json.load(open(os.path.join(d, "meta.json")))
    dur = meta["duration"]
    N = int(dur * SR)
    final = os.path.join(OUTPUT, os.path.splitext(os.path.basename(clip_path))[0] + "_final.mp4")

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
        L = min(wav.shape[1], N - s0)
        voice[:, s0:s0 + L] = wav[:, :L]
        extra_chunks.append([round(s0 / SR, 3), round((s0 + L) / SR, 3)])
        over = (s0 + L) / SR - float(tk["end"])
        emit("retake", f"TTS in place at {tk['start']:.2f}s — “{tk.get('text','')[:60]}”" +
             (f" — runs {over:.2f}s past its window" if over > 0.3 else ""),
             level="warn" if over > 0.3 else "ok",
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
        voice[:, s0:e0] = ae.dialogue_norm(voice[:, s0:e0], settings,
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
            base = base * float(np.clip((10 ** (settings.dialog_db / 20.0)) / a_meas,
                                        10 ** (-24 / 20.0), 10 ** (24 / 20.0)))
            voice_meas_db = 20.0 * np.log10(ae.active_rms(
                np.concatenate([base[:, int(a * SR):min(N, int(b * SR))]
                                for a, b in list(chunk_list) + extra_chunks], axis=1)) + 1e-12)
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


# ---------------------------------------------------------------- reel

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
    out = os.path.join(OUTPUT, "reel.mp4")
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
    tmp = os.path.join(OUTPUT, "reel_stitch.mp4")
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
