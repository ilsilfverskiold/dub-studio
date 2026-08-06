"""Dub Studio local server — stdlib only.

The pipeline is STAGED and the user can inspect/adjust between stages:
  upload -> detect (free)  -> edit regions & voice count (re-detect at any sensitivity, free)
         -> cast (~2c/clip) -> audition/swap voices, fix region labels
         -> convert (paid, gated) -> mix (free knobs, per-clip or master) -> render master / reel

v2: multiple PROJECTS (each fully self-contained: uploads/state/output/cache), per-clip mix
overrides on top of a master mix, take history, in-app API keys (written to dub_studio/.env).

Endpoints:
  GET  /                     app
  GET  /api/status           full staged state (active project)
  GET  /api/projects         project list
  GET  /api/keys             key presence + masked tails (never full values)
  GET  /api/voices           ElevenLabs account voices with preview URLs
  GET  /api/takes?clip=name  take history + stems for one clip
  GET  /api/events           SSE console (scoped to the active project)
  GET  /media/out/<f>  /media/<f>  /media/solo/<hash>/<voice|bed>  /media/take/<hash>/<f>
  POST /api/projects         {"action": "create"|"open"|"rename"|"delete", "id"?, "name"?}
  POST /api/keys             {"elevenlabs"?, "gemini"?, "huggingface"?} -> written to .env
  POST /api/upload           raw body + X-Filename
  POST /api/reorder          {"clips": [names in new order]}
  POST /api/remove           {"clip": name}
  POST /api/analyze          detect stage (free)
  POST /api/redetect         {"clip", "sensitivity": 0..100} — re-derive regions from cache, free
  POST /api/regions          {"clip", "regions":[{start,end}...], "voices": n}
  POST /api/identify         {"approved": true} cast stage (~2c/clip needing it)
  POST /api/voice            {"identifier", "voice_id"} — swap a character's voice
  POST /api/kinds            {"clip", "kinds": {"idx": "kind"}}
  POST /api/assign           {"clip", "assigns": [...]}
  POST /api/char             {"action": "add"|"rename"|"update", ...}
  POST /api/convert          {"approved": true} the paid stage
  POST /api/remix            {"settings": {...}} free — master mix change
  POST /api/clip-mix         {"clip", "settings": {...}} or {"clip", "match_master": true}
  POST /api/render-master    bounce the full program (free — nothing re-converts)
  POST /api/take             {"clip", "action": "snapshot"|"star"|"unstar"|"delete", "id"?}
  POST /api/reset            clear the ACTIVE project's clips + results + cast
  POST /api/clear-cache      wipe the ACTIVE project's caches (re-billing applies)
  POST /api/duck             timeline bars + cuts: {"clip", "regions"?, "boost_regions"?,
                             "voice_dips"?, "voice_mutes"?, "voice_splits"?, "tts_takes"?}
                             (bars carry per-bar "db"; audio-affecting saves auto-render, free)
  POST /api/transcribe       {"clip", "start", "end", "approved"} Gemini window transcript (paid)
  POST /api/tts-retake       {"clip", "index", "text", "approved", ...} generate a TTS take (paid)
  GET  /api/level            ?clip=name | ?master=1 — measured loudness envelope (the amber floor)
"""

import hashlib
import json
import os
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

import pipeline as pl
import projects
import providers as pv
from audio_engine import SR, Settings

HERE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = None                     # set by _open_project()

STATE = {"project": None, "clips": [], "settings": Settings().to_dict(), "busy": False, "reel": None}
EVENTS = {}                        # pid -> [event, ...]
_EV_LOCK = threading.Lock()
_TLS = threading.local()           # background jobs pin their project here (see _job)
_VOICES_CACHE = {"t": 0, "voices": []}


def _pid():
    # A job thread is pinned to the project it was started for; everything else follows the
    # active project. This is what keeps one project's events out of another project's log.
    return getattr(_TLS, "pid", None) or (STATE["project"] or {}).get("id") or "_"


def _log_path(pid):
    return os.path.join(projects.project_dir(pid), "logs.jsonl")


def emit(clip, stage, message, level="info", metrics=None, detail=None):
    """message = one human sentence (what is happening and why). detail = the mono under-the-hood
    line (provider · model · numbers) the UI shows beneath it — the logs teach, not just report.
    Every event is also appended to the project's logs.jsonl so history survives restarts."""
    pid = _pid()
    ev = {"t": time.time(), "clip": os.path.basename(clip) if clip else "",
          "stage": stage, "message": message, "level": level, "metrics": metrics or {},
          "detail": detail or ""}
    with _EV_LOCK:
        EVENTS.setdefault(pid, []).append(ev)
    if pid != "_":
        try:
            with open(_log_path(pid), "a") as f:
                f.write(json.dumps(ev) + "\n")
        except OSError:
            pass


def _load_log_history(pid):
    """Read the project's persisted log back into memory (last 400 events), trimming the file
    if it has grown past 1200 lines."""
    if pid in EVENTS:
        return
    evs = []
    try:
        with open(_log_path(pid)) as f:
            lines = [l for l in f.readlines() if l.strip()]
        if len(lines) > 1200:
            lines = lines[-400:]
            with open(_log_path(pid), "w") as f:
                f.writelines(lines)
        evs = [json.loads(l) for l in lines[-400:]]
    except (OSError, json.JSONDecodeError):
        evs = []
    EVENTS[pid] = evs


def _clip_emit(clip):
    return lambda stage, message, level="info", metrics=None, detail=None: \
        emit(clip, stage, message, level, metrics, detail)


def _job(fn):
    pid = _pid()
    STATE["busy"] = True               # set BEFORE the thread starts — the project-switch gate
                                       # must never see a gap between "job accepted" and "busy"
    def run():
        _TLS.pid = pid                 # emits from this job land in ITS project's log, even if
                                       # the active project somehow changes mid-run
        try:
            fn()
        except Exception as e:
            emit("", "error", f"{e}", level="error")
            traceback.print_exc()
        finally:
            STATE["busy"] = False
        _kick_remix()                  # timeline edits saved while this job ran render now
    threading.Thread(target=run, daemon=True).start()


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


def _final_path(clip):
    return os.path.join(pl.OUTPUT, os.path.splitext(os.path.basename(clip))[0] + "_final.mp4")


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


def _master_settings():
    return Settings.from_dict(STATE["settings"])


def _settings_hash(d):
    return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


def _emit_settings_diff(clip, old, new, warn_if_same=True):
    """Log exactly which knobs changed, old -> new — computed from what is actually stored,
    so the log doubles as proof the change took. Returns True if anything changed."""
    diffs = [f"{k} {old.get(k)} → {v}" for k, v in new.items() if old.get(k) != v]
    if diffs:
        emit(clip, "mix", "settings changed: " + " · ".join(diffs),
             level="info", detail="every mix from now on renders with these values — old takes keep theirs")
    elif warn_if_same:
        emit(clip, "mix", "no setting actually changed — the faders match what's already rendered",
             level="warn")
    return bool(diffs)


# ------------------------------------------------ project lifecycle

def _open_project(pid):
    global UPLOADS
    meta = projects.activate(pid)
    UPLOADS = os.path.join(projects.project_dir(pid), "uploads")
    os.makedirs(UPLOADS, exist_ok=True)
    files = {f: os.path.join(UPLOADS, f) for f in sorted(os.listdir(UPLOADS))
             if f.lower().endswith(projects.VIDEO_EXTS)}
    order = [f for f in meta.get("clip_order", []) if f in files]
    order += [f for f in sorted(files) if f not in order]
    STATE["clips"] = [files[f] for f in order]
    STATE["settings"] = {**Settings().to_dict(), **(meta.get("settings") or {})}
    STATE["reel"] = None
    STATE["project"] = {"id": pid, "name": meta.get("name", pid)}
    _load_log_history(pid)
    emit("", "project", f"opened \"{meta.get('name')}\" — {len(order)} clip(s)", level="ok")
    # LAW MIGRATION (once per project): the Background fader became an ABSOLUTE level — values
    # stored under the old meaning must never be silently reinterpreted. Reset to equal-the-
    # voice and say so; the user sets it where they want from there.
    if (meta.get("mix_migrated") or 0) < 4:   # the law-4 fader re-meaning, once per project EVER
        old_bed = STATE["settings"].get("bed_db")
        STATE["settings"]["bed_db"] = STATE["settings"].get("dialog_db", -16.0)
        for c in STATE["clips"]:
            ov = pl.get_clip_override(c)
            if ov is not None:
                ov["bed_db"] = ov.get("dialog_db", STATE["settings"]["bed_db"])
                pl.set_clip_override(c, ov)
        meta["mix_migrated"] = pl.MIX_LAW
        meta["settings"] = STATE["settings"]
        projects.write_meta(pid, meta)
        emit("", "mix", "the Background fader now means an ABSOLUTE level (same scale as the "
             f"voice) — reset from {old_bed} to {STATE['settings']['bed_db']:.1f}, equal to the "
             "voice fader. Move it where you want it; the number is the measured level",
             level="warn")
    # STALE CHECK on open — flag only, never render here (auto-rendering on open stalled the
    # whole app). The master button lights up; ONE free click applies everything pending.
    # Edits the user makes still render themselves through the queue as always.
    master = _master_settings()
    behind = [c for c in STATE["clips"]
              if pl._read_json(os.path.join(pl.clip_state_dir(c), "meta.json"))
              and pl.mix_stale(c, master)]
    if behind:
        emit("", "mix", f"{len(behind)} clip(s) have sound updates pending — press "
             "\"Render master · free\" to apply them (no credits)", level="info")


def _save_project(reel_meta=None):
    pid = _pid()
    if pid == "_":
        return
    try:
        meta = projects.read_meta(pid)
    except (OSError, json.JSONDecodeError):
        return
    meta["clip_order"] = [os.path.basename(c) for c in STATE["clips"]]
    meta["settings"] = STATE["settings"]
    if reel_meta is not None:
        meta["reel_meta"] = reel_meta
    projects.write_meta(pid, meta)


def _reel_meta():
    try:
        return projects.read_meta(_pid()).get("reel_meta") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _master_state():
    """none: nothing to assemble yet. stale: something changed since the last render. ok: current."""
    master = _master_settings()
    reel = os.path.join(pl.OUTPUT, "reel.mp4")
    finals = [c for c in STATE["clips"] if os.path.exists(_final_path(c))]
    rm = _reel_meta()
    if not finals:
        return {"state": "none", "rendered_at": None}
    if not rm.get("rendered_at"):
        return {"state": "stale", "rendered_at": None}
    stale = False
    if rm.get("clip_order") != [os.path.basename(c) for c in STATE["clips"]]:
        stale = True
    if rm.get("settings_hash") != _settings_hash(STATE["settings"]):
        stale = True
    for c in STATE["clips"]:
        d = pl.clip_state_dir(c)
        if not pl._read_json(os.path.join(d, "meta.json")):
            stale = True                                   # convert-stale
        elif pl.mix_stale(c, master):
            stale = True
    if os.path.exists(reel):
        mt = [os.path.getmtime(_final_path(c)) for c in finals]
        if mt and max(mt) > os.path.getmtime(reel) + 1:
            stale = True
    elif len(finals) > 1:
        stale = True
    return {"state": "stale" if stale else "ok", "rendered_at": rm.get("rendered_at")}


# ------------------------------------------------ status

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
                           os.path.join(d, "tts", t.get("file", "") or "_"))}
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


# ------------------------------------------------ runners

def run_analyze(targets=None):
    todo = [c for c in (targets or STATE["clips"])
            if not pl._read_json(os.path.join(pl.clip_state_dir(c), "analysis.json"))]
    if not todo:
        emit("", "detect", "all clips are already detected — nothing to do", level="ok")
        return
    for c in todo:
        pl.analyze(c, _clip_emit(c))
    emit("", "detect", f"{len(todo)} clip(s) detected — review regions and voice count, then Cast",
         level="ok")


def run_identify(targets=None):
    n = 0
    for c in (targets or STATE["clips"]):
        d = pl.clip_state_dir(c)
        if not pl._read_json(os.path.join(d, "analysis.json")):
            pl.analyze(c, _clip_emit(c))
        if not pl._read_json(os.path.join(d, "who.json")):
            pl.identify_clip(c, _clip_emit(c))
            n += 1
    if n == 0:
        emit("", "cast", "already cast — nothing to do", level="ok")
    else:
        usd = credits = 0
        for c in (targets or STATE["clips"]):
            ana = pl._read_json(os.path.join(pl.clip_state_dir(c), "analysis.json")) or {}
            usd += ana.get("el_usd", 0)
            credits += ana.get("estimated_credits", 0)
        emit("", "cast", f"cast ready ({n} clip(s)) — audition the voices, swap any you don't like, "
             f"then Convert ≈ ${usd:.2f} (≈ {credits} ElevenLabs credits; cached steps are free)",
             level="ok")


def run_convert(targets=None):
    master = _master_settings()
    if targets:
        todo = targets                                    # explicitly asked — run even if converted
    else:
        todo = [c for c in STATE["clips"] if pl.clip_stage(c) != "converted"]
        skipped = len(STATE["clips"]) - len(todo)
        if skipped:
            emit("", "convert", f"skipping {skipped} already-converted clip(s) — nothing re-bills",
                 level="cache")
    if not todo:
        emit("", "convert", "everything is already converted — nothing to do", level="ok")
        return
    for c in todo:
        d = pl.clip_state_dir(c)
        if not pl._read_json(os.path.join(d, "analysis.json")):
            pl.analyze(c, _clip_emit(c))
        if not pl._read_json(os.path.join(d, "who.json")):
            pl.identify_clip(c, _clip_emit(c))
        pl.convert_clip(c, _clip_emit(c), pl.effective_settings(c, master))
    _rebuild_reel(master)
    emit("", "done", "converted — listen below; the sound panel re-mixes for free", level="ok")


def _rebuild_reel(master):
    finals = [_final_path(c) for c in STATE["clips"]
              if pl.clip_stage(c) == "converted" and os.path.exists(_final_path(c))]
    if len(finals) > 1:
        STATE["reel"] = pl.make_reel(finals, master, _clip_emit(""))
    _save_project(reel_meta={"rendered_at": time.time(),
                             "clip_order": [os.path.basename(c) for c in STATE["clips"]],
                             "settings_hash": _settings_hash(STATE["settings"])})


def run_render_master():
    """Free: every clip whose mix is out of date is re-mixed with its EFFECTIVE settings
    (master ⊕ its own override), then the program is re-stitched. Nothing re-converts."""
    if _master_state()["state"] == "ok":
        emit("", "done", "the master is already current — no fader moved and no clip changed, "
             "so there is nothing to render", level="ok")
        return
    master = _master_settings()
    n = 0
    for c in STATE["clips"]:
        if not pl._read_json(os.path.join(pl.clip_state_dir(c), "meta.json")):
            continue
        if pl.mix_stale(c, master) or not os.path.exists(_final_path(c)):
            pl.remix(c, pl.effective_settings(c, master), _clip_emit(c))
            n += 1
    _rebuild_reel(master)
    emit("", "done", f"master rendered — {n} clip mix(es) refreshed, no credits spent", level="ok")


def run_remix_one(clip):
    master = _master_settings()
    if pl._read_json(os.path.join(pl.clip_state_dir(clip), "meta.json")):
        pl.remix(clip, pl.effective_settings(clip, master), _clip_emit(clip))
    emit("", "done", "re-mix complete (no credits spent) — the master is stale until you re-render it",
         level="ok")


# Timeline edits render THEMSELVES (free stage only — nothing here ever converts or bills).
# A queue instead of a direct job: edits saved DURING a render must render after it, and a
# burst of edits to one clip coalesces into one remix. The set holds clip paths.
_PENDING_REMIX = set()
_RM_LOCK = threading.Lock()


def _queue_remix(clip):
    with _RM_LOCK:
        _PENDING_REMIX.add(clip)
    _kick_remix()


def _kick_remix():
    with _RM_LOCK:
        if STATE["busy"] or not _PENDING_REMIX:
            return                     # the running job re-kicks when it finishes (see _job)
        _job(_drain_remixes)


def _drain_remixes():
    while True:
        with _RM_LOCK:
            if not _PENDING_REMIX:
                return
            clip = _PENDING_REMIX.pop()
        run_remix_one(clip)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    # ------------------------------------------------ GET
    def do_GET(self):
        path, _, q = self.path.partition("?")
        query = {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}
        if path == "/":
            page = open(os.path.join(HERE, "static", "index.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")   # the app must NEVER be served stale
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        elif path in ("/docs/presentation", "/docs/presentation.html"):
            page = open(os.path.join(HERE, "docs", "presentation.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        elif path.startswith("/docs/") and path.endswith(".mp4") and "/" not in path[6:]:
            fp = os.path.join(HERE, "docs", path[6:])
            if os.path.exists(fp):
                data = open(fp, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(404)
        elif path == "/api/status":
            clips = [_clip_status(c) for c in STATE["clips"]]
            finals = [c for c in clips if "final" in c]
            reel = os.path.join(pl.OUTPUT, "reel.mp4")
            mt = [os.path.getmtime(_final_path(c)) for c in STATE["clips"] if os.path.exists(_final_path(c))]
            if os.path.exists(reel) and len(finals) > 1:
                mt.append(os.path.getmtime(reel))
            page_mtime = os.path.getmtime(os.path.join(HERE, "static", "index.html"))
            self._json({"v": 16, "page_mtime": page_mtime, "keys": pv.keys_status(), "clips": clips,
                        "cast": pl.load_cast(),   # PROJECT-wide cast — any character can TTS in any clip
                        "project": STATE["project"], "projects": projects.list_all(),
                        "master": _master_state(), "defaults": Settings().to_dict(),
                        "reel": "/media/out/reel.mp4" if (os.path.exists(reel) and len(finals) > 1) else None,
                        "settings": STATE["settings"], "busy": STATE["busy"],
                        "out_mtime": max(mt) if mt else 0})
        elif path == "/api/level":
            # measured loudness envelope of what's actually rendered — the timeline's level lane
            if query.get("master"):
                fpath, rendered = os.path.join(pl.OUTPUT, "reel.mp4"), True
            else:
                clip = next((c for c in STATE["clips"]
                             if os.path.basename(c) == os.path.basename(query.get("clip", ""))), None)
                if not clip:
                    self._json({"error": "unknown clip"}, 404)
                    return
                fin = _final_path(clip)
                rendered = os.path.exists(fin)
                fpath = fin if rendered else clip              # no final yet -> source level
            if not os.path.exists(fpath):
                self._json({"points": [], "floor": -60, "rendered": False})
                return
            self._json({"points": _level_points(fpath), "floor": -60, "rendered": rendered})
        elif path == "/api/projects":
            self._json({"projects": projects.list_all(), "active": _pid()})
        elif path == "/api/keys":
            self._json({"keys": pv.keys_masked()})
        elif path == "/api/takes":
            clip = next((c for c in STATE["clips"]
                         if os.path.basename(c) == os.path.basename(query.get("clip", ""))), None)
            if not clip:
                self._json({"error": "unknown clip"}, 404)
                return
            t = pl.load_takes(clip)
            h = pv.file_hash(clip)
            d = pl.clip_state_dir(clip)
            takes = [{**tk, "url": f"/media/take/{h}/{tk['file']}",
                      "starred": tk["id"] == t.get("starred")} for tk in t.get("takes", [])]
            stems = []
            if os.path.exists(os.path.join(d, "solo_voice.wav")):
                stems = [{"id": "stem_voice", "kind": "stem", "name": "Voice stem",
                          "url": f"/media/solo/{h}/voice"},
                         {"id": "stem_bed", "kind": "stem", "name": "Background stem",
                          "url": f"/media/solo/{h}/bed"}]
            self._json({"takes": takes, "stems": stems, "starred": t.get("starred")})
        elif path == "/api/voices":
            if time.time() - _VOICES_CACHE["t"] > 300:
                try:
                    _VOICES_CACHE["voices"] = [
                        {"voice_id": v.get("voice_id"), "name": v.get("name"),
                         "labels": v.get("labels") or {}, "preview": v.get("preview_url")}
                        for v in pv.el_voices()]
                    _VOICES_CACHE["t"] = time.time()
                except Exception as e:
                    self._json({"error": f"could not list voices: {e}"}, 502)
                    return
            self._json({"voices": _VOICES_CACHE["voices"]})
        elif path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            pid0 = _pid()
            try:
                # a browser auto-reconnect sends the last id it saw — resume there instead of
                # replaying the whole history into a console that already shows it
                sent = int(self.headers.get("Last-Event-ID", "")) + 1
            except ValueError:
                sent = 0
            try:
                while True:
                    if _pid() != pid0:      # project switched -> end this stream, client reconnects
                        self.wfile.write(b'data: {"stage":"project","clip":"","message":"switched","level":"info"}\n\n')
                        self.wfile.flush()
                        return
                    with _EV_LOCK:
                        evs = EVENTS.get(pid0, [])
                        if len(evs) < sent:  # the log shrank (project cleared) -> start over
                            sent = 0
                        pending = evs[sent:]
                        base = sent
                        sent = len(evs)
                    for i, ev in enumerate(pending):
                        self.wfile.write(f"id: {base + i}\ndata: {json.dumps(ev)}\n\n".encode())
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                return
        elif path.startswith("/media/"):
            self._media(path)
        else:
            self._json({"error": "not found"}, 404)

    def _media(self, path):
        rel = path[len("/media/"):]
        ctype = "video/mp4"
        if rel.startswith("out/"):
            fpath = os.path.join(pl.OUTPUT, os.path.basename(rel[4:]))
        elif rel.startswith("solo/"):
            parts = rel.split("/")
            if len(parts) != 3 or parts[2] not in ("voice", "bed"):
                self._json({"error": "not found"}, 404)
                return
            fpath = os.path.join(pl.STATE, os.path.basename(parts[1]), f"solo_{parts[2]}.wav")
            ctype = "audio/wav"
        elif rel.startswith("take/"):
            parts = rel.split("/")
            if len(parts) != 3:
                self._json({"error": "not found"}, 404)
                return
            fpath = os.path.join(pl.STATE, os.path.basename(parts[1]), "takes", os.path.basename(parts[2]))
        else:
            fpath = os.path.join(UPLOADS, os.path.basename(rel))
        if not os.path.exists(fpath):
            self._json({"error": "not found"}, 404)
            return
        size = os.path.getsize(fpath)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            end = int(b) if b else size - 1
        with open(fpath, "rb") as f:
            f.seek(start)
            data = f.read(end - start + 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "bytes")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------ POST
    def do_POST(self):
        route = self.path.split("?", 1)[0]
        gated = ("/api/remove", "/api/reset", "/api/clear-cache", "/api/regions", "/api/analyze",
                 "/api/identify", "/api/convert", "/api/remix", "/api/reorder", "/api/kinds",
                 "/api/voice", "/api/assign", "/api/char", "/api/clip-mix", "/api/render-master",
                 "/api/take", "/api/redetect", "/api/projects")
        # /api/duck is deliberately NOT busy-gated: duck/mute edits are a tiny JSON write that the
        # NEXT render reads — blocking them during a render made removals silently snap back.
        if route in gated and STATE["busy"]:
            self._json({"error": "a run is in progress — wait for it to finish"}, 409)
            return

        if route == "/api/upload":
            name = os.path.basename(self.headers.get("X-Filename") or f"clip_{int(time.time())}.mp4")
            if not name.lower().endswith(projects.VIDEO_EXTS):
                self._json({"error": "unsupported file type — video files only (.mp4 .mov .m4v .webm)"}, 400)
                return
            n = int(self.headers.get("Content-Length") or 0)
            p = os.path.join(UPLOADS, name)
            open(p, "wb").write(self.rfile.read(n))
            if p not in STATE["clips"]:
                STATE["clips"].append(p)
            _save_project()
            emit(p, "upload", f"{name} uploaded ({os.path.getsize(p)//1024} KB)")
            self._json({"ok": True})
        elif route == "/api/projects":
            b = self._body()
            action = b.get("action", "")
            try:
                if action == "create":
                    pid = projects.create(b.get("name", ""))
                    _open_project(pid)
                elif action == "open":
                    _open_project(b.get("id", ""))
                elif action == "rename":
                    projects.rename(b.get("id") or _pid(), b.get("name", ""))
                    if (b.get("id") or _pid()) == _pid():
                        STATE["project"]["name"] = projects.read_meta(_pid()).get("name")
                    emit("", "project", f"renamed to \"{b.get('name')}\"", level="ok")
                elif action == "delete":
                    target = b.get("id", "")
                    if not target:
                        self._json({"error": "missing project id"}, 400)
                        return
                    was_active = target == _pid()
                    nxt = projects.delete(target)
                    emit("", "project", "project deleted", level="warn")
                    if was_active:
                        _open_project(nxt)
                else:
                    self._json({"error": "unknown action"}, 400)
                    return
            except RuntimeError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json({"ok": True, "active": _pid(), "projects": projects.list_all()})
        elif route == "/api/keys":
            b = self._body()
            status = pv.set_keys({k: b.get(k) for k in ("elevenlabs", "gemini", "huggingface")})
            saved = [k for k in ("elevenlabs", "gemini", "huggingface") if (b.get(k) or "").strip()]
            if saved:
                emit("", "keys", f"{' + '.join(saved)} key saved to dub_studio/.env — used from the next call on",
                     level="ok")
            self._json({"keys": pv.keys_masked(), "status": status})
        elif route == "/api/reorder":
            names = self._body().get("clips", [])
            by = {os.path.basename(c): c for c in STATE["clips"]}
            if set(names) == set(by):
                STATE["clips"] = [by[n] for n in names]
                _save_project()
                emit("", "clips", "order updated: " + " -> ".join(names))
                self._json({"ok": True})
            else:
                self._json({"error": "clip list mismatch"}, 400)
        elif route == "/api/remove":
            name = os.path.basename(self._body().get("clip", ""))
            path = os.path.join(UPLOADS, name)
            STATE["clips"] = [c for c in STATE["clips"] if os.path.basename(c) != name]
            if os.path.exists(path):
                os.remove(path)
            _save_project()
            emit("", "clips", f"removed {name}")
            self._json({"ok": True})
        elif route == "/api/analyze":
            b = self._body()
            if not pv.keys_status()["huggingface"]:
                self._json({"error": "detection needs a Hugging Face token — add it under API keys "
                            "in the sidebar (free; used to load the diarization model)",
                            "missing": ["huggingface"]}, 403)
                return
            targets, name = _resolve_target(b)
            if name and targets is None:
                self._json({"error": "unknown clip"}, 404)
                return
            _job(lambda: run_analyze(targets))
            self._json({"ok": True})
        elif route == "/api/redetect":
            b = self._body()
            clip = _clip_by_name(b)
            if not clip:
                self._json({"error": "unknown clip"}, 404)
                return
            try:
                regions, voices = pl.redetect(clip, int(b.get("sensitivity", 100)))
            except RuntimeError as e:
                self._json({"error": str(e)}, 400)
                return
            emit(clip, "detect", f"re-derived at sensitivity {int(b.get('sensitivity', 100))}% — "
                 f"{len(regions)} region(s), {voices} voice(s); cast kept", level="ok")
            self._json({"ok": True, "regions": regions, "voices": voices})
        elif route == "/api/regions":
            b = self._body()
            clip = _clip_by_name(b)
            if not clip:
                self._json({"error": "unknown clip"}, 404)
                return
            pl.save_detection_edits(clip, regions=b.get("regions"), voices=b.get("voices"))
            n = len(b.get("regions") or [])
            emit(clip, "detect", f"regions saved ({n}) — cast kept; Convert will use the new timing",
                 level="ok")
            self._json({"ok": True})
        elif route == "/api/identify":
            b = self._body()
            if not b.get("approved"):
                self._json({"error": "identifying the cast uses Gemini (~$0.02 per clip) — approve in the UI"}, 403)
                return
            if not pv.keys_status()["gemini"]:
                self._json({"error": "casting needs a Gemini API key — add it under API keys in the sidebar",
                            "missing": ["gemini"]}, 403)
                return
            targets, name = _resolve_target(b)
            if name and targets is None:
                self._json({"error": "unknown clip"}, 404)
                return
            if b.get("force") and targets:
                # explicit re-cast: archive the current take (if any), then let Gemini redo the cast
                for c in targets:
                    pl.archive_take(c, "recast", _final_path(c))
                    d = pl.clip_state_dir(c)
                    for f in ("who.json", "meta.json"):
                        p = os.path.join(d, f)
                        if os.path.exists(p):
                            os.remove(p)
                emit(targets[0], "cast", "re-casting from scratch — Gemini watches the clip again; "
                     "the previous version was archived to Layers first", level="info")
            _job(lambda: run_identify(targets))
            self._json({"ok": True})
        elif route == "/api/voice":
            b = self._body()
            if not b.get("voice_id", "").strip():
                self._json({"error": "empty voice id"}, 400)
                return
            persisted = pl.set_cast_voice(b.get("identifier", ""), b["voice_id"])
            if persisted is not None:
                ident = pl._norm_id(b.get("identifier", ""))
                stale = 0
                for c in STATE["clips"]:
                    d = pl.clip_state_dir(c)
                    who = pl._read_json(os.path.join(d, "who.json"))
                    mp = os.path.join(d, "meta.json")
                    who_ids = {pl._norm_id(k) for k in (who or {}).get("voice_of", {})}
                    if who and ident in who_ids and os.path.exists(mp):
                        os.remove(mp)
                        stale += 1
                # log what is actually ON DISK now, re-read after the write — never the request.
                # If these ever differ the bug is back, and this line is the tripwire.
                vname = _voice_name(persisted)
                mismatch = "" if persisted == b["voice_id"].strip() else \
                           f" (WARNING: requested {b['voice_id'].strip()} but disk holds {persisted})"
                emit("", "cast", f"@{ident} voice persisted as \"{vname}\"{mismatch}" +
                     (f" — {stale} clip(s) will re-convert with it" if stale else ""),
                     level="warn" if mismatch else "ok")
                self._json({"ok": True, "voice_id": persisted})
            else:
                self._json({"error": "unknown character"}, 404)
        elif route == "/api/assign":
            b = self._body()
            clip = _clip_by_name(b)
            if not clip:
                self._json({"error": "unknown clip"}, 404)
                return
            try:
                pl.save_assign_edits(clip, b.get("assigns", []))
            except RuntimeError as e:
                self._json({"error": str(e)}, 400)
                return
            _K = {"speech": "words", "singing": "singing", "human_nonword": "vocal", "animal_vocal": "animal", "non_speech": "SFX"}
            mapping = " · ".join(f"[{i+1}] {a.get('character') or '—'}/{_K.get(a.get('kind'), '?')}"
                                 for i, a in enumerate(b.get("assigns", [])))
            emit(clip, "cast", f"who speaks where: {mapping or '(none)'}", level="ok")
            self._json({"ok": True})
        elif route == "/api/char":
            b = self._body()
            clip = _clip_by_name(b) if b.get("clip") else None
            try:
                r = pl.char_edit(b.get("action", ""), b.get("identifier", ""),
                                 new_identifier=b.get("new_identifier"), clip_path=clip,
                                 updates=b.get("fields"))
            except RuntimeError as e:
                self._json({"error": str(e)}, 400)
                return
            if b.get("action") == "update":
                emit(clip or "", "cast", f"@{b.get('identifier')} updated — {r}", level="ok")
                self._json({"ok": True})
                return
            if b.get("action") == "rename":
                emit(clip or "", "cast", f"renamed @{b.get('identifier')} -> @{b.get('new_identifier')} everywhere",
                     level="ok")
            else:
                emit(clip or "", "cast", f"added @{b.get('identifier')} with starter voice \"{r}\" — "
                     "press Change on its row to pick your own", level="ok")
            self._json({"ok": True})
        elif route == "/api/kinds":
            b = self._body()
            clip = _clip_by_name(b)
            if not clip:
                self._json({"error": "unknown clip"}, 404)
                return
            try:
                pl.save_kind_edits(clip, b.get("kinds", {}))
            except RuntimeError as e:
                self._json({"error": str(e)}, 400)
                return
            emit(clip, "cast", "region labels updated — re-run Convert to apply", level="ok")
            self._json({"ok": True})
        elif route == "/api/convert":
            b = self._body()
            if not b.get("approved"):
                self._json({"error": "converting spends ElevenLabs credits — approve it in the UI"}, 403)
                return
            if not pv.keys_status()["elevenlabs"]:
                self._json({"error": "converting needs an ElevenLabs API key — add it under API keys "
                            "in the sidebar", "missing": ["elevenlabs"]}, 403)
                return
            if b.get("settings"):
                STATE["settings"].update(Settings.from_dict(b["settings"]).to_dict())
                _save_project()
            targets, name = _resolve_target(b)
            if name and targets is None:
                self._json({"error": "unknown clip"}, 404)
                return
            _job(lambda: run_convert(targets))
            self._json({"ok": True})
        elif route == "/api/remix":
            b = self._body()
            if b.get("settings"):
                old = dict(STATE["settings"])
                STATE["settings"].update(Settings.from_dict(b["settings"]).to_dict())
                _save_project()
                # identical faders are NOT a warning here — duck/mute edits render through this
                # same button, and run_render_master reports honestly what it did
                _emit_settings_diff("", old, STATE["settings"], warn_if_same=False)
            _job(run_render_master)
            self._json({"ok": True})
        elif route == "/api/clip-mix":
            b = self._body()
            clip = _clip_by_name(b)
            if not clip:
                self._json({"error": "unknown clip"}, 404)
                return
            changed = True
            if b.get("match_master"):
                pl.set_clip_override(clip, None)
                emit(clip, "mix", "matched to the master mix — per-clip offsets cleared", level="ok")
            elif b.get("settings"):
                old = pl.effective_settings(clip, _master_settings()).to_dict()
                pl.set_clip_override(clip, b["settings"])
                new = pl.effective_settings(clip, _master_settings()).to_dict()
                changed = _emit_settings_diff(clip, old, new)
            else:
                self._json({"error": "nothing to change"}, 400)
                return
            if pl._read_json(os.path.join(pl.clip_state_dir(clip), "meta.json")) and \
                    (changed or pl.mix_stale(clip, _master_settings())):
                _job(lambda: run_remix_one(clip))
            self._json({"ok": True})
        elif route == "/api/duck":
            b = self._body()
            clip = _clip_by_name(b)
            if not clip:
                self._json({"error": "unknown clip"}, 404)
                return
            ep = os.path.join(pl.clip_state_dir(clip), "edits.json")
            ed = pl._read_json(ep) or {}
            # converted clip -> the edit renders itself below; before convert there is nothing
            # to render yet and the edit simply waits for the first convert
            converted = bool(pl._read_json(os.path.join(pl.clip_state_dir(clip), "meta.json")))
            when = "in the render starting now" if converted else "on the next render"
            def _bars(rows):
                # bars carry their own adjustable amount ("db"); absent -> the fader default
                out = []
                for r in (rows or []):
                    if float(r.get("end", 0)) <= float(r.get("start", 0)):
                        continue
                    row = {"start": round(float(r["start"]), 2), "end": round(float(r["end"]), 2)}
                    if r.get("db") is not None:
                        row["db"] = round(abs(float(r["db"])), 1)
                    out.append(row)
                return out
            if "regions" in b:
                regions = _bars(b.get("regions"))
                ed["duck_regions"] = regions
                ed["duck_owned"] = bool(b.get("owned", True))
                if ed["duck_owned"]:
                    emit(clip, "mix", f"duck map is yours now ({len(regions)} bar(s)) — "
                         f"each dips by its own amount {when}", level="ok",
                         detail=" · ".join(f"{r['start']:.2f}-{r['end']:.2f}s" for r in regions) or "none")
                else:
                    emit(clip, "mix", "duck bars will be re-proposed on the next render — "
                         "drawn on the timeline where voices speak, yours to edit or delete",
                         level="ok")
            if "boost_regions" in b:
                br = _bars(b.get("boost_regions"))
                ed["boost_regions"] = br
                emit(clip, "mix", f"boost bars saved ({len(br)}) — each lifts by its own amount "
                     f"{when}", level="ok",
                     detail=" · ".join(f"{r['start']:.2f}-{r['end']:.2f}s" for r in br) or "none")
            if "voice_dips" in b:
                vd = _bars(b.get("voice_dips"))
                ed["voice_dips"] = vd
                emit(clip, "mix", f"original-voice turn-down(s) saved ({len(vd)}) — the original "
                     f"voice layer dips by each region's amount {when}", level="ok",
                     detail=" · ".join(f"{r['start']:.2f}-{r['end']:.2f}s" for r in vd) or "none")
            if "tts_takes" in b:
                prev = ed.get("tts_takes", []) or []
                tl = []
                for idx, t in enumerate(b.get("tts_takes") or []):
                    if float(t.get("end", 0)) > float(t.get("start", 0)):
                        row = {"start": round(float(t["start"]), 2),
                               "end": round(float(t["end"]), 2),
                               "text": (t.get("text") or "")[:2000],
                               "file": os.path.basename(t.get("file") or "")}
                        if t.get("character"):
                            row["character"] = pl._norm_id(str(t["character"]))[:80]
                        # a stale client must never erase a generated take's audio
                        if not row["file"] and idx < len(prev) and prev[idx].get("file"):
                            row["file"] = prev[idx]["file"]
                        if t.get("speed"):
                            row["speed"] = round(float(t["speed"]), 2)
                        if t.get("place_at") is not None:
                            row["place_at"] = round(float(t["place_at"]), 2)
                        tl.append(row)
                ed["tts_takes"] = tl
                emit(clip, "retake", f"TTS window(s) saved ({len(tl)}) — select one and Generate "
                     "to give it a voice", level="ok",
                     detail=" · ".join(f"{t['start']:.2f}-{t['end']:.2f}s" for t in tl) or "none")
            if "voice_splits" in b:
                ed["voice_splits"] = sorted({round(float(t), 2) for t in (b.get("voice_splits") or [])})
            if "voice_mutes" in b:
                vm = [[round(float(a), 2), round(float(z), 2)] for a, z in (b.get("voice_mutes") or [])
                      if float(z) > float(a)]
                ed["voice_mutes"] = vm
                emit(clip, "mix", f"voice mutes saved ({len(vm)}) — the new voice goes silent there "
                     f"{when}; click the striped line to bring it back", level="ok",
                     detail=" · ".join(f"{a:.2f}-{z:.2f}s" for a, z in vm) or "none")
            pl._write_json(ep, ed)
            # what you hear must always match the timeline: audio-affecting edits render
            # themselves (free). voice_splits alone are just markers — nothing to render.
            if converted and any(k in b for k in ("regions", "boost_regions", "voice_mutes",
                                                  "tts_takes", "voice_dips")):
                _queue_remix(clip)
            self._json({"ok": True})
        elif route == "/api/transcribe":
            b = self._body()
            if not b.get("approved"):
                self._json({"error": "transcribing uses Gemini (~$0.01) — approve in the UI"}, 403)
                return
            if not pv.keys_status()["gemini"]:
                self._json({"error": "transcription needs a Gemini API key — add it under API keys",
                            "missing": ["gemini"]}, 403)
                return
            clip = _clip_by_name(b)
            if not clip:
                self._json({"error": "unknown clip"}, 404)
                return
            w0, w1 = float(b.get("start", 0)), float(b.get("end", 0))
            if w1 <= w0:
                self._json({"error": "bad window"}, 400)
                return
            # the PROVEN pass reads lips + audio, so it gets the VIDEO slice, not just sound
            who = pl._read_json(os.path.join(pl.clip_state_dir(clip), "who.json")) or {}
            seg = os.path.join(pv.WORK, f"tr_{pv.file_hash(clip)}_{int(w0*100)}.mp4")
            pv.ff(["-y", "-ss", str(w0), "-to", str(w1), "-i", clip,
                   "-vf", "scale=360:-2,fps=15", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "28", "-c:a", "aac", "-b:a", "96k", seg], check=True)
            try:
                lines = pv.gemini_window_script(seg, who.get("chars", []), emit=_clip_emit(clip))
            except RuntimeError as e:
                self._json({"error": str(e)}, 502)
                return
            finally:
                if os.path.exists(seg):
                    os.remove(seg)
            text = " ".join((l.get("text") or "").strip() for l in lines).strip()
            tot = sum(max(0.0, float(l["end"]) - float(l["start"])) for l in lines) or 1.0
            speed = round(sum(float(l.get("speed", 1.0)) *
                              max(0.0, float(l["end"]) - float(l["start"])) for l in lines) / tot, 2)                 if lines else 1.0
            place_at = round(w0 + float(lines[0]["start"]), 2) if lines else round(w0, 2)
            emit(clip, "retake", f"{w0:.2f}-{w1:.2f}s transcript: “{text or '(no words heard)'}” — "
                 f"correct it before generating · pace ×{speed} · speech starts at {place_at:.2f}s",
                 level="ok",
                 detail="the proven dubbing-script pass: exact words, syllable timing, and pace — temperature 0")
            self._json({"ok": True, "text": text, "speed": speed, "place_at": place_at})
        elif route == "/api/tts-retake":
            b = self._body()
            clip = _clip_by_name(b)
            if not clip:
                self._json({"error": "unknown clip"}, 404)
                return
            if not b.get("approved"):
                self._json({"error": "generating speech spends ElevenLabs credits — approve in the UI"}, 403)
                return
            if not pv.keys_status()["elevenlabs"]:
                self._json({"error": "TTS needs an ElevenLabs API key — add it under API keys",
                            "missing": ["elevenlabs"]}, 403)
                return
            ep = os.path.join(pl.clip_state_dir(clip), "edits.json")
            ed = pl._read_json(ep) or {}
            takes = ed.get("tts_takes", []) or []
            i = int(b.get("index", -1))
            if not (0 <= i < len(takes)):
                self._json({"error": "unknown TTS window"}, 404)
                return
            text = (b.get("text") or "").strip()
            if not text:
                self._json({"error": "no text — nothing to speak"}, 400)
                return
            # voice: the character assigned to the region under this window (midpoint), else the lead
            who = pl._read_json(os.path.join(pl.clip_state_dir(clip), "who.json")) or {}
            voice_of = {pl._norm_id(k): v for k, v in (who.get("voice_of") or {}).items()}
            for cch in pl.load_cast():
                if pl._norm_id(cch["identifier"]) in voice_of:
                    voice_of[pl._norm_id(cch["identifier"])] = cch["voice_id"]
            ident = pl._norm_id(b.get("character") or takes[i].get("character") or "")
            if not ident:
                mid = (takes[i]["start"] + takes[i]["end"]) / 2.0
                regions, _, _ = pl.effective_detection(clip)
                assigns = who.get("assigns", [])
                for ri, r in enumerate(regions):
                    if r["start"] <= mid <= r["end"] and ri < len(assigns):
                        ident = pl._norm_id(assigns[ri].get("character") or "")
                        break
            if not ident:
                ident = pl._norm_id((who.get("chars") or [{}])[0].get("identifier", ""))
            vid = voice_of.get(ident)
            if not vid:
                # the character was never cast in THIS clip — any project character may speak here
                vid = next((cch["voice_id"] for cch in pl.load_cast()
                            if pl._norm_id(cch["identifier"]) == ident), None)
            if not vid:
                self._json({"error": "no voice for this window — cast the clip or add the character first"}, 400)
                return
            if b.get("speed"):
                takes[i]["speed"] = round(max(0.7, min(1.2, float(b["speed"]))), 2)
            if b.get("place_at") is not None:
                try:
                    takes[i]["place_at"] = round(float(b["place_at"]), 2)
                except (TypeError, ValueError):
                    pass
            try:
                wav = pv.el_tts(text, vid, emit=_clip_emit(clip),
                                speed=takes[i].get("speed", 1.0))
            except Exception as e:
                self._json({"error": f"TTS failed: {e}"}, 502)
                return
            tdir = os.path.join(pl.clip_state_dir(clip), "tts")
            os.makedirs(tdir, exist_ok=True)
            fn = f"w{i}_{hashlib.sha1((text + vid).encode()).hexdigest()[:8]}.wav"
            pv.write_audio(os.path.join(tdir, fn), wav)
            takes[i]["text"] = text
            takes[i]["file"] = fn
            takes[i]["character"] = ident
            ed["tts_takes"] = takes
            pl._write_json(ep, ed)
            emit(clip, "retake", f"TTS ready for {takes[i]['start']:.2f}-{takes[i]['end']:.2f}s "
                 f"(“{text[:60]}”) — rendering the clip now, free", level="ok",
                 detail=f"@{ident} · {len(text)} characters · {wav.shape[1]/44100:.2f}s of audio")
            _queue_remix(clip)         # renders now — or right after the run in progress finishes
            self._json({"ok": True})
        elif route == "/api/render-master":
            _job(run_render_master)
            self._json({"ok": True})
        elif route == "/api/take":
            b = self._body()
            clip = _clip_by_name(b)
            if not clip:
                self._json({"error": "unknown clip"}, 404)
                return
            action = b.get("action", "")
            try:
                if action == "snapshot":
                    tid = pl.archive_take(clip, "snapshot", _final_path(clip))
                    if not tid:
                        self._json({"error": "no finished take to snapshot yet"}, 400)
                        return
                    emit(clip, "takes", "current take snapshotted — it survives future converts/mixes",
                         level="ok")
                elif action == "star":
                    has_stem = pl.star_take(clip, b.get("id", ""), _final_path(clip))
                    if has_stem:
                        emit(clip, "takes", "take pinned — ITS voice is used, and the mix rebuilds "
                             "around it with the current rules (rendering now, free)", level="ok")
                        _queue_remix(clip)
                    else:
                        emit(clip, "takes", "take pinned — an older take with no separate voice "
                             "track, so its finished file plays AS-IS (today's cleaning can't "
                             "reach inside it)", level="warn")
                elif action == "unstar":
                    pl.clear_star(clip)
                    emit(clip, "takes", "take unstarred — re-mixing to the live settings", level="ok")
                    _job(lambda: run_remix_one(clip))
                elif action == "delete":
                    pl.delete_take(clip, b.get("id", ""))
                    emit(clip, "takes", "take deleted", level="ok")
                else:
                    self._json({"error": "unknown action"}, 400)
                    return
            except RuntimeError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json({"ok": True})
        elif route == "/api/reset":
            for c in list(STATE["clips"]):
                if os.path.exists(c):
                    os.remove(c)
            for f in os.listdir(pl.OUTPUT):
                os.remove(os.path.join(pl.OUTPUT, f))
            STATE["clips"].clear()
            STATE["reel"] = None
            pl.reset_cast()
            import shutil as _sh
            for dname in os.listdir(pl.STATE):
                dpath = os.path.join(pl.STATE, dname)
                if os.path.isdir(dpath):
                    _sh.rmtree(dpath)
            _save_project(reel_meta={"rendered_at": None, "clip_order": [], "settings_hash": ""})
            with _EV_LOCK:
                EVENTS[_pid()] = []               # a cleared project starts with a clean log too
            try:
                os.remove(_log_path(_pid()))
            except OSError:
                pass
            emit("", "reset", "project cleared — its clips, results and state removed (other projects untouched)")
            self._json({"ok": True})
        elif route == "/api/clear-cache":
            # USER DATA IS NEVER CACHE. This once rmtree'd the whole state dir and silently
            # destroyed every take (unrecoverable). Now it deletes ONLY paid-result caches and
            # derived render files — takes, edits, casts, who-speaks-where and TTS audio stay.
            import shutil
            n = 0
            for sub in ("iso", "sts", "sep", "tts"):
                p = os.path.join(pv.CACHE, sub)
                if os.path.isdir(p):
                    n += len(os.listdir(p))
                    shutil.rmtree(p)
            derived = ("analysis.json", "meta.json", "mix.wav", "src.wav",
                       "voice_raw.wav", "solo_voice.wav", "solo_bed.wav")
            if os.path.isdir(pl.STATE):
                for dname in os.listdir(pl.STATE):
                    dpath = os.path.join(pl.STATE, dname)
                    if not os.path.isdir(dpath):
                        continue
                    for f in derived:
                        fp = os.path.join(dpath, f)
                        if os.path.exists(fp):
                            os.remove(fp)
            emit("", "cache", f"THIS project's caches cleared ({n} entries) — takes, edits, cast "
                 "and TTS audio KEPT; the next run is a full run and will re-bill", level="warn")
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def main():
    migrated = projects.migrate_legacy()
    reg = projects.load_registry()
    _open_project(reg["active"])
    if migrated:
        emit("", "project", "existing session moved into this project — everything is intact",
             level="ok")
    port = int(os.environ.get("PORT", 8765))
    print(f"Dub Studio -> http://localhost:{port}  (project: {STATE['project']['name']})")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
