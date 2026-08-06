"""External services and caching for Dub Studio.

Every paid call (ElevenLabs isolation / STS) is cached by a hash of its exact input, so repeating a run
with unchanged inputs is free, and any change that alters the input forces a fresh call — honestly.
Gemini and the local models (pyannote diarization, audio-separator) are wrapped here too.
"""

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

import imageio_ffmpeg
import numpy as np
from pedalboard.io import AudioFile

HERE = os.path.dirname(os.path.abspath(__file__))
SR = 44100
CACHE = os.path.join(HERE, "cache")     # per-project since v2 — server calls set_cache_dir() on project open
WORK = os.path.join(HERE, "work")       # shared scratch
os.makedirs(WORK, exist_ok=True)


def set_cache_dir(path):
    """Point the paid-result caches (iso/sts/sep) at the active project. Per-project by design:
    each project is fully self-contained; re-using a clip in another project re-bills (the
    approval gate still fronts every paid run)."""
    global CACHE
    CACHE = path
    os.makedirs(CACHE, exist_ok=True)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
GEMINI_MODEL = "gemini-3.5-flash"
STS_MODEL = "eleven_multilingual_sts_v2"

# ---------------------------------------------------------------- keys / env

def _load_env():
    """dub_studio/.env first, then the project root .env as fallback."""
    out = {}
    for path in (os.path.join(HERE, ".env"), os.path.join(HERE, "..", ".env")):
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    out.setdefault(k.strip().lower(), v.strip().strip('"').strip("'"))
    return out

_ENV = _load_env()

def env(key):
    return os.environ.get(key.upper()) or os.environ.get(key) or _ENV.get(key.lower())

def keys_status():
    return {
        "elevenlabs": bool(env("elevenlabs_api_key") or env("elevenlabs")),
        "gemini": bool(env("gemini_api_key")),
        "huggingface": bool(env("hf_token")),
    }


ENV_PATH = os.path.join(HERE, ".env")
_KEYMAP = {"elevenlabs": "ELEVENLABS_API_KEY", "gemini": "GEMINI_API_KEY", "huggingface": "HF_TOKEN"}
_KEYENV = {"elevenlabs": "elevenlabs_api_key", "gemini": "gemini_api_key", "huggingface": "hf_token"}


def keys_masked():
    """Key PRESENCE only. No value, no fragment, no length ever leaves the server —
    the UI shows just 'key set' / 'no key'."""
    out = {}
    for name in _KEYMAP:
        v = env(_KEYENV[name]) or (env("elevenlabs") if name == "elevenlabs" else None)
        out[name] = {"set": bool(v)}
    return out


def set_keys(updates):
    """Write keys into dub_studio/.env (create if missing) and load them live — no restart.
    Atomic write, unknown lines preserved verbatim, chmod 600. ONLY this file is ever written;
    the parent ../.env read-fallback stays untouched (it holds unrelated keys)."""
    lines = open(ENV_PATH).read().splitlines() if os.path.exists(ENV_PATH) else []
    for name, value in (updates or {}).items():
        if name not in _KEYMAP or not (value or "").strip():
            continue
        value = value.strip()
        K = _KEYMAP[name]
        for i, l in enumerate(lines):
            st = l.strip()
            if st and not st.startswith("#") and "=" in st and \
                    st.split("=", 1)[0].strip().upper() == K:
                lines[i] = f"{K}={value}"
                break
        else:
            lines.append(f"{K}={value}")
        _ENV[_KEYENV[name]] = value            # live immediately, no restart
        os.environ[K] = value
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, ENV_PATH)
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass
    return keys_status()

# ---------------------------------------------------------------- ffmpeg / audio io

def ff(args, check=False):
    r = subprocess.run([FFMPEG, *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"ffmpeg failed: {' '.join(args)}\n{r.stderr[-600:]}")
    return r

def media_duration(path):
    r = ff(["-i", path])
    for line in r.stderr.splitlines():
        if "Duration" in line:
            h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return None

def read_audio(path, channels=2, sr=SR):
    wav = os.path.join(WORK, f"rd_{uuid.uuid4().hex[:8]}.wav")
    ff(["-y", "-i", path, "-vn", "-ac", str(channels), "-ar", str(sr), wav], check=True)
    with AudioFile(wav) as f:
        audio = f.read(f.frames)
    os.remove(wav)
    if audio.shape[0] == 1 and channels == 2:
        audio = np.vstack([audio, audio])
    return audio

def write_audio(path, audio, sr=SR):
    with AudioFile(path, "w", sr, audio.shape[0]) as f:
        f.write(audio.astype(np.float32))

def file_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]

def _ensure_ffmpeg_on_path():
    """audio-separator shells out to `ffmpeg` by name; point it at the bundled binary."""
    d = os.path.join(tempfile.gettempdir(), "ffbin_dubstudio")
    os.makedirs(d, exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        link = os.path.join(d, name)
        if not os.path.exists(link):
            try:
                os.symlink(FFMPEG, link)
            except OSError:
                pass
    if d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

# ---------------------------------------------------------------- ElevenLabs (paid, cached)

def _el_post(url, key, files=None, data=None, timeout=180):
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in (data or {}).items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    for k, (fname, blob, mime) in (files or {}).items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                 f"Content-Type: {mime}\r\n\r\n").encode() + blob + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("xi-api-key", key)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(4 * (attempt + 1))
                continue
            raise RuntimeError(f"ElevenLabs HTTP {e.code}: {detail}")

def el_isolate(wav_path, emit=None):
    """Voice isolation, cached by input file hash. Returns path to the isolated stem wav."""
    key = env("elevenlabs_api_key") or env("elevenlabs")
    cdir = os.path.join(CACHE, "iso"); os.makedirs(cdir, exist_ok=True)
    out = os.path.join(cdir, file_hash(wav_path) + ".wav")
    if os.path.exists(out):
        if emit: emit("isolate", "voice isolation cached from an identical earlier run — no credits spent",
                      level="cache", detail="results are keyed by a hash of the exact input audio · same input = $0")
        return out
    if emit: emit("isolate", "isolating the voice from the full soundtrack — only this isolated "
                  "voice gets re-voiced; music and effects never enter the voice model", level="paid",
                  detail="elevenlabs audio isolation · paid · cached by input hash")
    blob = _el_post("https://api.elevenlabs.io/v1/audio-isolation", key,
                    files={"audio": (os.path.basename(wav_path), open(wav_path, "rb").read(), "audio/wav")})
    tmp = out + ".bin"
    open(tmp, "wb").write(blob)
    ff(["-y", "-i", tmp, "-ac", "2", "-ar", str(SR), out], check=True)
    os.remove(tmp)
    return out

def el_sts(seg_audio, voice_id, emit=None):
    """Speech-to-speech, cached by hash(input audio bytes + voice id). Returns stereo np array."""
    key = env("elevenlabs_api_key") or env("elevenlabs")
    cdir = os.path.join(CACHE, "sts"); os.makedirs(cdir, exist_ok=True)
    h = hashlib.sha1(np.ascontiguousarray(seg_audio).tobytes() + voice_id.encode()).hexdigest()[:16]
    cpath = os.path.join(cdir, h + ".wav")
    if os.path.exists(cpath):
        if emit: emit("sts", "this exact audio + voice was converted before — no credits spent",
                      level="cache", detail="cached by hash(audio + voice id) · $0")
        with AudioFile(cpath) as f:
            out = f.read(f.frames)
        return out if out.shape[0] == 2 else np.vstack([out, out])
    if emit: emit("sts", f"re-performing {seg_audio.shape[1]/SR:.1f}s in the new voice — words, rhythm "
                  "and emotion come from the original audio, not from a script", level="paid",
                  detail=f"elevenlabs speech-to-speech · {STS_MODEL} · paid · cached by input hash")
    seg = os.path.join(WORK, f"sts_in_{h}.wav")
    write_audio(seg, seg_audio)
    blob = _el_post(f"https://api.elevenlabs.io/v1/speech-to-speech/{voice_id}", key,
                    files={"audio": (os.path.basename(seg), open(seg, "rb").read(), "audio/wav")},
                    data={"model_id": STS_MODEL, "remove_background_noise": "true"})
    os.remove(seg)
    tmp = cpath + ".bin"
    open(tmp, "wb").write(blob)
    wav = cpath + ".t.wav"
    ff(["-y", "-i", tmp, "-ac", "2", "-ar", str(SR), wav], check=True)
    with AudioFile(wav) as f:
        out = f.read(f.frames)
    os.remove(tmp)
    out = out if out.shape[0] == 2 else np.vstack([out, out])
    write_audio(cpath, out)
    os.remove(wav)
    return out

TTS_MODEL = "eleven_multilingual_v2"


def el_tts(text, voice_id, emit=None, speed=1.0):
    """Text-to-speech in a specific voice — the retake tool, with the PROVEN v1 delivery settings:
    Gemini-derived speed (clamped to ElevenLabs' 0.7..1.2) so the line takes as long to say as the
    original did. Cached by hash(text + voice + model + speed) — same line again is free."""
    key = env("elevenlabs_api_key") or env("elevenlabs")
    speed = round(max(0.7, min(1.2, float(speed or 1.0))), 2)
    cdir = os.path.join(CACHE, "tts"); os.makedirs(cdir, exist_ok=True)
    h = hashlib.sha1(f"{text}|{voice_id}|{TTS_MODEL}|{speed}".encode()).hexdigest()[:16]
    cpath = os.path.join(cdir, h + ".wav")
    if os.path.exists(cpath):
        if emit: emit("retake", "this exact line + voice was generated before — no credits spent",
                      level="cache", detail="cached by hash(text + voice) · $0")
        with AudioFile(cpath) as f:
            out = f.read(f.frames)
        return out if out.shape[0] == 2 else np.vstack([out, out])
    if emit: emit("retake", f"speaking {len(text)} characters in the character's voice at pace "
                  f"×{speed} — a fresh performance from your text, not a conversion", level="paid",
                  detail=f"elevenlabs text-to-speech · {TTS_MODEL} · ≈{len(text)} credits · cached")
    vs = {"stability": 0.42, "similarity_boost": 0.8, "style": 0.35, "use_speaker_boost": True,
          "speed": speed}
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128",
        data=json.dumps({"text": text, "model_id": TTS_MODEL, "voice_settings": vs}).encode(),
        method="POST")
    req.add_header("xi-api-key", key)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        blob = r.read()
    tmp = cpath + ".bin"
    open(tmp, "wb").write(blob)
    wav = cpath + ".t.wav"
    ff(["-y", "-i", tmp, "-ac", "2", "-ar", str(SR), wav], check=True)
    with AudioFile(wav) as f:
        out = f.read(f.frames)
    os.remove(tmp); os.remove(wav)
    out = out if out.shape[0] == 2 else np.vstack([out, out])
    write_audio(cpath, out)
    return out


GEMINI_FPS = 15
_EMOTIONS = ["neutral", "happy", "sad", "angry", "surprised", "fearful", "disgusted", "excited", "deadpan"]


def gemini_window_script(video_slice, characters, emit=None):
    """The PROVEN syllable pass from the original pipeline (voice_v1 gemini_script), verbatim:
    exact words incl. faint vocalizations, start/end per line, PACE derived from syllables vs
    duration, and every syllable's timing. Temperature 0. Returns the lines list."""
    if emit: emit("retake", "Gemini reads the window frame by frame — exact words, exact syllable "
                  "timing, and how fast they're said", level="paid",
                  detail=f"{GEMINI_MODEL} · {GEMINI_FPS} fps · temperature 0 · the proven dubbing-script pass")
    gk = env("gemini_api_key")
    clist = ", ".join(f"{c['identifier']} ({c.get('description','')})" for c in characters) or "@speaker"
    prompt = (
        f"Read this animated clip (sampled {GEMINI_FPS} fps, audio present) and produce the DUBBING "
        f"SCRIPT so it can be re-voiced with the SAME timing and delivery. Speaking characters: {clist}.\n"
        "Return `lines` IN ORDER. A line = one continuous spoken phrase by ONE character; start a new "
        "line when the speaker changes or the mouth clearly pauses. For each line give EXACTLY:\n"
        "- character: which @identifier speaks it (from the list above).\n"
        "- text: EXACTLY what the character voices, real spelling ('Buddy', not 'Bud dy'). This "
        "includes spoken WORDS and the character's non-word VOCALIZATIONS — 'hmm', 'uh', 'oh', 'mmm', "
        "a hum, a sigh, a laugh, a gasp, a groan. LISTEN TO THE AUDIO closely for FAINT, quiet "
        "vocalizations that are easy to miss — a soft hum, a breath, an intake of air, a small 'mm' "
        "or 'hmm', a quiet sigh under or between the words — and INCLUDE them even if they are brief "
        "or low-volume; do not skip a sound just because it is faint. KEY RULE: the user chose this "
        "exact clip on purpose to be re-voiced — transcribe EVERY sound a character makes with its "
        "voice, INCLUDING animal calls (a moo, a meow, a bark, a roar): write them as speakable text "
        "exactly as voiced ('moooo', 'meooow') with their timing. Nothing vocal here is excluded.\n"
        "- start, end: seconds (2 decimals) — the exact frames the mouth moves for this line.\n"
        "- speed: how fast the words come out on THIS line, number 0.70..1.20. DERIVE it from the "
        "timing you assign: count the syllables vs how long start->end is. Few words drawn out over a "
        "long time = slow (0.72-0.85); many words in a short time = fast (1.10-1.20); otherwise ~1.00. "
        "Do NOT put 1.00 on every line — lines paced differently MUST get different numbers.\n"
        "- emotion: the ACTUAL delivered emotion you see+hear (face and voice) — exactly one of "
        "neutral, happy, sad, angry, surprised, fearful, disgusted, excited, deadpan. Use 'deadpan' "
        "for flat comedic delivery; only use 'neutral' if there is genuinely no emotional colour.\n"
        "- syllables: the line broken into syllables in order, each {text, start, end} (exact lip timing)."
    )
    syl = {"type": "object", "properties": {
        "text": {"type": "string"}, "start": {"type": "number"}, "end": {"type": "number"}},
        "required": ["text", "start", "end"]}
    line = {"type": "object", "properties": {
        "character": {"type": "string"}, "text": {"type": "string"},
        "start": {"type": "number"}, "end": {"type": "number"},
        "speed": {"type": "number"}, "emotion": {"type": "string", "enum": _EMOTIONS},
        "syllables": {"type": "array", "items": syl}},
        "required": ["character", "text", "start", "end", "speed", "emotion", "syllables"]}
    schema = {"type": "object", "properties": {"lines": {"type": "array", "items": line}},
              "required": ["lines"]}
    b64 = base64.b64encode(open(video_slice, "rb").read()).decode()
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": "video/mp4", "data": b64},
         "video_metadata": {"fps": GEMINI_FPS}},
        {"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "responseSchema": schema, "temperature": 0}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={gk}"
    data = json.dumps(body).encode()
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                out = json.loads(json.loads(r.read())["candidates"][0]["content"]["parts"][0]["text"])
                return out.get("lines", [])
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"Gemini HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}")


def el_voices():
    key = env("elevenlabs_api_key") or env("elevenlabs")
    req = urllib.request.Request("https://api.elevenlabs.io/v1/voices")
    req.add_header("xi-api-key", key)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read()).get("voices", [])

def pick_voice(gender, age, language="", exclude=()):
    """Pick an account voice by labels: gender first, then language, then age. Deterministic."""
    voices = el_voices()
    scored = []
    for v in voices:
        lab = {k.lower(): (val or "").lower() for k, val in (v.get("labels") or {}).items()}
        score = 0
        if gender in ("male", "female") and lab.get("gender") == gender:
            score += 4
        elif gender in ("male", "female") and lab.get("gender") in ("male", "female"):
            score -= 4
        if language and language not in ("unknown", "") and language in lab.get("language", ""):
            score += 3
        if age and age != "not-important" and age.replace("_", " ") in lab.get("age", ""):
            score += 2
        if v.get("voice_id") in exclude:
            score -= 10
        scored.append((score, v.get("name"), v.get("voice_id"), lab.get("gender", "")))
    scored.sort(key=lambda t: -t[0])
    if not scored:
        raise RuntimeError("no ElevenLabs voices on the account")
    _, name, vid, vgender = scored[0]
    return vid, name, vgender

# ---------------------------------------------------------------- Gemini (pennies, not gated)

def gemini(video_path, prompt, schema):
    gk = env("gemini_api_key")
    b64 = base64.b64encode(open(video_path, "rb").read()).decode()
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": "video/mp4", "data": b64}}, {"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "responseSchema": schema, "temperature": 0}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={gk}"
    data = json.dumps(body).encode()
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(json.loads(r.read())["candidates"][0]["content"]["parts"][0]["text"])
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"Gemini HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}")

# ---------------------------------------------------------------- local models (free)

_DIARIZER = None

def diarize(video_path, emit=None):
    """pyannote speaker diarization (local, free): regions with speaker labels + voice count."""
    global _DIARIZER
    import torch
    if _DIARIZER is None:
        if emit: emit("diarize", "loading the speaker-diarization model — first time downloads it, "
                      "afterwards it starts instantly",
                      detail="pyannote/speaker-diarization-community-1 · gated model, your HF token authorizes it")
        from pyannote.audio import Pipeline as PyPipeline
        _DIARIZER = PyPipeline.from_pretrained("pyannote/speaker-diarization-community-1",
                                               token=env("hf_token"))
    audio = read_audio(video_path, channels=1)
    wave = torch.tensor(audio[:1, :], dtype=torch.float32)
    diar = _DIARIZER({"waveform": wave, "sample_rate": SR})
    regions = []
    for turn, _, spk in diar.speaker_diarization.itertracks(yield_label=True):
        regions.append({"start": round(turn.start, 2), "end": round(turn.end, 2), "speaker": spk})
    regions.sort(key=lambda r: r["start"])
    speakers = sorted({r["speaker"] for r in regions})
    return regions, speakers

_SEPARATOR = None

# two separation models, user's choice via `separation_model=` in dub_studio/.env:
#   mdx      (default) — UVR-MDX-NET Voc_FT: fast, decent
#   roformer           — BS-RoFormer: best available quality, slower, ~600 MB one-time download
SEP_MODELS = {"mdx": "UVR-MDX-NET-Voc_FT.onnx",
              "roformer": "model_bs_roformer_ep_317_sdr_12.9755.ckpt"}
_SEP_LOADED = None


def sep_model_key():
    k = (env("separation_model") or "mdx").strip().lower()
    return k if k in SEP_MODELS else "mdx"


def separate(video_path, emit=None):
    """Local source separation (free): returns (instrumental_path, vocals_path).
    Cached by file hash AND model — switching models re-separates, old stems are kept aside."""
    global _SEPARATOR, _SEP_LOADED
    key = sep_model_key()
    cdir = os.path.join(CACHE, "sep"); os.makedirs(cdir, exist_ok=True)
    h = file_hash(video_path)
    inst = os.path.join(cdir, f"{h}_{key}_inst.flac")
    voc = os.path.join(cdir, f"{h}_{key}_voc.flac")
    if os.path.exists(inst) and os.path.exists(voc):
        if emit: emit("separate", "voice/background stems already on disk — separation skipped",
                      level="cache", detail=f"cached by file hash · {key} model · local · $0")
        return inst, voc
    _ensure_ffmpeg_on_path()
    if _SEPARATOR is None or _SEP_LOADED != key:
        if emit: emit("separate", f"loading the {key} separation model (local, free)" +
                      (" — first use downloads it, this can take a while" if key == "roformer" else ""))
        from audio_separator.separator import Separator
        _SEPARATOR = Separator(output_dir=os.path.join(WORK, "sep"), output_format="FLAC")
        try:
            _SEPARATOR.load_model(model_filename=SEP_MODELS[key])
            _SEP_LOADED = key
        except Exception as e:
            # a bad download or offline machine must never kill a render — fall back, say so
            if key != "mdx":
                if emit: emit("separate", f"the {key} model failed to load ({str(e)[:80]}…) — "
                              "falling back to mdx for this run; check the download and try again",
                              level="warn")
                key = "mdx"
                inst = os.path.join(cdir, f"{h}_{key}_inst.flac")
                voc = os.path.join(cdir, f"{h}_{key}_voc.flac")
                if os.path.exists(inst) and os.path.exists(voc):
                    return inst, voc
                _SEPARATOR.load_model(model_filename=SEP_MODELS[key])
                _SEP_LOADED = key
            else:
                raise
    wav = os.path.join(WORK, f"sep_in_{h}.wav")
    ff(["-y", "-i", video_path, "-vn", "-ac", "2", "-ar", str(SR), wav], check=True)
    if emit: emit("separate", "splitting the soundtrack into VOICE and BACKGROUND — the background "
                  "becomes the bed under the new voice",
                  detail=f"{sep_model_key()} · local · free — a real separation model, because subtracting "
                         "an isolated voice from the mix leaves the original voice audible")
    outs = _SEPARATOR.separate(wav)
    os.remove(wav)
    import shutil
    for f in outs:
        p = f if os.path.isabs(f) else os.path.join(WORK, "sep", f)
        if "(Instrumental)" in os.path.basename(p):
            shutil.copyfile(p, inst)
        elif "(Vocals)" in os.path.basename(p):
            shutil.copyfile(p, voc)
    if not (os.path.exists(inst) and os.path.exists(voc)):
        raise RuntimeError(f"separation did not produce both stems: {outs}")
    return inst, voc
