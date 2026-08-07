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


from . import store
from .cast import (ARCHES, _norm_id, char_edit, identify, identify_clip, load_cast,
                   lock_voices, reset_cast, save_assign_edits, save_cast, save_kind_edits,
                   set_cast_voice)
from .convert import convert_clip
from .detect import analyze, postprocess_regions, redetect, save_detection_edits
from .mix import remix
from .overrides import (_keep_off_windows, effective_settings, get_clip_override,
                        mix_stale, set_clip_override)
from .reel import make_reel
from .store import (MIX_LAW, _read_json, _write_json, clip_stage, clip_state_dir,
                    effective_detection, set_project_dirs)
from .takes import (archive_take, clear_star, delete_take, load_takes, star_take,
                    takes_dir)


def __getattr__(name):
    # STATE/OUTPUT are REBOUND per project by set_project_dirs — always read them live
    if name in ("STATE", "OUTPUT"):
        return getattr(store, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
