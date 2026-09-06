"""Shared ground: per-project dirs, atomic state-file IO, stage reads."""

import json
import os
import sys
import threading

import providers as pv


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
# v12 (2026-09-03): the voice canvas runs the tone chain at a FIXED internal level (CHAIN_DB)
#   and the dialog fader is applied at the final level-set — at a low fader the compressor
#   used to switch off silently and TTS takes played with unshaved peaks (audibly louder).
MIX_LAW = 12

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


def clip_state_dir(clip_path):
    d = os.path.join(STATE, pv.file_hash(clip_path))
    os.makedirs(d, exist_ok=True)
    return d




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


