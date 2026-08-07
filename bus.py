"""App-wide state and machinery: the STATE dict, the per-project event log,
background jobs (with the busy gate), and the free-remix queue."""

import json
import os
import threading
import time
import traceback

import projects
from audio_engine import Settings

STATE = {"project": None, "clips": [], "settings": Settings().to_dict(), "busy": False,
         "reel": None}
EVENTS = {}                        # pid -> [event, ...]
_EV_LOCK = threading.Lock()
_TLS = threading.local()           # background jobs pin their project here (see _job)


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



# runners.py registers run_remix_one here at import — bus must not import runners
_REMIX_RUNNER = None


def set_remix_runner(fn):
    global _REMIX_RUNNER
    _REMIX_RUNNER = fn


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
        _REMIX_RUNNER(clip)


