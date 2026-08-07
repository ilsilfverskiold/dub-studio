"""The cast: character registry, who-speaks-where, Gemini identity casting."""

import os

import providers as pv

from . import store
from .store import _read_json, _write_json, clip_state_dir, effective_detection


ARCHES = ["animated boy", "animated girl", "animated character", "young boy", "young girl",
          "young man", "young woman", "middle-aged man", "old man", "old woman", "deep-voiced man"]




def _norm_id(s):
    """Gemini returns identifiers as 'name' or '@name' unpredictably; one canonical form everywhere."""
    return (s or "").strip().lstrip("@").strip()


def load_cast():
    p = os.path.join(store.STATE, "cast.json")
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
    _write_json(os.path.join(store.STATE, "cast.json"), cast)


def reset_cast():
    p = os.path.join(store.STATE, "cast.json")
    if os.path.exists(p):
        os.remove(p)


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


def char_edit(action, identifier, new_identifier=None, clip_path=None, updates=None,
              voice_id=None):
    """User-controlled cast: add a character yourself, or rename one everywhere (registry + clips)."""
    identifier = _norm_id(identifier)
    cast = load_cast()
    if action == "add":
        if any(_norm_id(c["identifier"]) == identifier for c in cast):
            raise RuntimeError(f"@{identifier} already exists")
        if voice_id and str(voice_id).strip():
            # the caller chose the voice up front (the TTS modal's inline add) — no starter pick
            vid, name, vg = str(voice_id).strip(), None, ""
        else:
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
        for dname in os.listdir(store.STATE):
            wp = os.path.join(store.STATE, dname, "who.json")
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
        for dname in os.listdir(store.STATE):
            wp = os.path.join(store.STATE, dname, "who.json")
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
        for dname in os.listdir(store.STATE):
            who = _read_json(os.path.join(store.STATE, dname, "who.json"))
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


