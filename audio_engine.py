"""The audio-engineering layer of Dub Studio — every knob the UI exposes lives here.

This layer is pure math over cached material (raw STS output + separated stems), so changing any
setting and re-mixing costs nothing. The defaults encode what weeks of listening tests settled on;
the comments say why, so you can disagree with reasons in view.
"""

from dataclasses import dataclass, asdict, fields

import numpy as np
from pedalboard import (Compressor, HighpassFilter, HighShelfFilter, Limiter,
                        LowpassFilter, LowShelfFilter, Pedalboard, PeakFilter, Reverb)

SR = 44100


@dataclass
class Settings:
    # --- dialogue ---
    dialog_db: float = -16.0      # constant dialogue level (active speech RMS). EVERY line lands here —
                                  # uniform loudness across spans and clips; the bed carries the mood.
    # --- the standard dialogue-post chain, one knob per canonical move ---
    hpf_hz: float = 110.0         # rumble filter: cut everything below (stacked x2 = 12 dB/oct)
    bass_shelf_db: float = -4.5   # boom shelf below ~220 Hz (proximity/boom; measured +12 dB once)
    notch_hz: float = 900.0       # sweepable PROBLEM frequency — find the ugly tone by sweeping...
    notch_db: float = -2.5        # ...then cut it here. The core subtractive-EQ skill.
    presence_db: float = 0.0      # 4 kHz clarity/edge. Up = crisper but fatiguing
    air_db: float = 0.0           # 9 kHz+ shelf: openness/sheen
    deess_db: float = 0.0         # sibilance control: max dB of reduction on harsh S sounds (5-9 kHz)
    comp_ratio: float = 2.0       # dynamics: evens syllables at constant loudness
    room_wet: float = 0.0         # ADR room glue: a touch seats the dry AI voice IN the scene;
                                  # too much = audible echo (slap-back) — learned the hard way
    # --- background bed ---
    bed_db: float = -16.0         # ABSOLUTE background level, same scale as dialog_db. The
                                  # number on the fader IS the measured level of the leveled
                                  # bed — same in every clip. Defaults start EQUAL to the
                                  # voice (law: the engine has no loudness opinions).
    bed_hpf_hz: float = 40.0      # bed low cut. 40 = just sub-rumble (the old fixed value);
                                  # sweep up when the background itself sounds muddy/bass-heavy
    bed_bass_db: float = 0.0      # bed boom shelf below ~220 Hz — tame a bassy bed without
                                  # gutting its warmth (the bed twin of bass_shelf_db)
    duck: bool = True             # duck the bed under the voice
    duck_under_db: float = -28.0  # target: residual original voice this far under the dub during speech
    # --- output ---
    reel_lufs: float = -18.0      # final delivery loudness of the stitched reel

    @classmethod
    def from_dict(cls, d):
        valid = {f.name for f in fields(cls)}
        clean = {}
        for k, v in (d or {}).items():
            if k in valid:
                clean[k] = bool(v) if k == "duck" else float(v)
        return cls(**clean)

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------- basic measures

def active_rms(audio, floor_db=-45.0):
    """RMS over the non-silent part only, so silence can't skew level decisions."""
    mono = np.mean(audio ** 2, axis=0)
    m = mono > (10 ** (floor_db / 20.0)) ** 2
    return float(np.sqrt(mono[m].mean())) if m.any() else 0.0


def rms_db(audio):
    return 20.0 * np.log10(float(np.sqrt(np.mean(audio ** 2))) + 1e-12)


def smooth_env(audio, win_ms=50):
    mono = np.mean(audio ** 2, axis=0) if audio.ndim == 2 else audio ** 2
    win = max(1, int(SR * win_ms / 1000))
    return np.sqrt(np.convolve(mono, np.ones(win) / win, mode="same"))


def voiced_chunks(audio, min_len_s=0.15, merge_gap_s=0.30, thr_frac=0.15):
    """Split audio into voiced (start,end) sample spans by its own energy envelope."""
    env = smooth_env(audio, 30)
    act = env[env > 1e-6]
    if not act.size:
        return []
    thr = float(np.percentile(act, 90)) * thr_frac
    on = env > thr
    # merge short gaps, then collect runs
    chunks, i = [], 0
    while i < len(on):
        if on[i]:
            j = i
            gap = 0
            k = i
            while k < len(on):
                if on[k]:
                    j, gap = k, 0
                else:
                    gap += 1
                    if gap > int(merge_gap_s * SR):
                        break
                k += 1
            if (j - i) / SR >= min_len_s:
                chunks.append((i, j + 1))
            i = k
        else:
            i += 1
    return chunks


# ---------------------------------------------------------------- dialogue chain

def voice_chain(s: Settings):
    """Tone shaping only: EQ + compression, NO limiter and NO level target here. Loudness is re-set
    AFTER this chain (finalize_voice), so every tonal knob works at constant loudness — a cut changes
    the TONE, not the volume. Filters are wide (q 0.7-0.9) so the knobs cover real problems, not a
    sliver of them. No reverb (slap-back on a dry solo voice), no makeup gain (loudness comes later)."""
    return Pedalboard([
        HighpassFilter(cutoff_frequency_hz=s.hpf_hz),
        HighpassFilter(cutoff_frequency_hz=s.hpf_hz),
        LowShelfFilter(cutoff_frequency_hz=220, gain_db=s.bass_shelf_db, q=0.7),
        PeakFilter(cutoff_frequency_hz=s.notch_hz, gain_db=s.notch_db, q=0.8),
        PeakFilter(cutoff_frequency_hz=4000, gain_db=s.presence_db, q=0.9),
        HighShelfFilter(cutoff_frequency_hz=9000, gain_db=s.air_db, q=0.7),
        Compressor(threshold_db=-18, ratio=s.comp_ratio, attack_ms=15, release_ms=160),
    ])


def deess(audio, amount_db):
    """Dynamic sibilance control: reduce ONLY the 4.5-9.5 kHz band, ONLY while it spikes (harsh S/T
    sounds). A static EQ cut here would dull the whole voice; a de-esser ducks just the spikes."""
    if amount_db <= 0:
        return audio
    band = Pedalboard([HighpassFilter(4500), LowpassFilter(9500)])(audio, SR)
    rest = audio - band
    env = smooth_env(band, 8)
    act = env[env > 1e-7]
    if not act.size:
        return audio
    thr = float(np.percentile(act, 95)) * 0.4
    over_db = 20 * np.log10(np.maximum(env / thr, 1.0))
    red_db = np.minimum(over_db, amount_db)
    w = max(1, int(SR * 0.005))
    red_db = np.convolve(red_db, np.ones(w) / w, mode="same")
    return rest + band * (10 ** (-red_db / 20.0))[None, :]


def finalize_voice(base, s: Settings):
    """AFTER tone shaping: de-ess, seat in the room, safety limiter. NO level-set here — the
    final level is set LAST, in remix, measured on the speech itself, so the fader number IS
    the measured loudness (the old in-here level-set drifted ~6 dB from the knob)."""
    base = deess(base, s.deess_db)
    if s.room_wet > 0:
        base = Reverb(room_size=0.22, damping=0.6, wet_level=float(s.room_wet),
                      dry_level=1.0, width=0.5)(base, SR)
    return Pedalboard([Limiter(threshold_db=-1.5, release_ms=150)])(base, SR)


def low_share_db(audio, hz=250.0):
    """Share of the signal's total energy below `hz`, in dB — the objective 'how boomy' number."""
    mono = np.mean(audio, axis=0)
    sp = np.abs(np.fft.rfft(mono)) ** 2
    freqs = np.fft.rfftfreq(mono.shape[0], 1.0 / SR)
    tot = float(sp.sum()) + 1e-12
    return 10.0 * np.log10(float(sp[freqs < hz].sum()) / tot + 1e-12)


def match_low_end(audio, ref_share_db, hz=250.0, max_cut_db=18.0):
    """Measured boom-matcher for TTS takes. TTS is a fresh synthesis and can arrive FAR boomier
    than STS (which re-performs the isolated stem with noise removal) — measured in the field:
    a TTS take at -2.8 dB low-end share next to STS dialogue at -9 to -25. The shared chain
    cleans both identically, but identical cleaning of unequal dirt sounds unequal. So: cut
    below `hz` by exactly how much this take EXCEEDS the reference dialogue — never boost,
    never guess. Returns (audio, cut_db_applied)."""
    total = 0.0
    for _ in range(3):   # share-vs-shelf-dB is nonlinear when boom dominates — iterate to target
        excess = low_share_db(audio, hz) - ref_share_db
        if excess <= 1.0 or total >= max_cut_db:
            break
        cut = min(excess, max_cut_db - total)
        audio = Pedalboard([LowShelfFilter(cutoff_frequency_hz=hz, gain_db=-cut, q=0.7)])(audio, SR)
        total += cut
    return audio, round(total, 1)



CHAIN_DB = -16.0
# The voice canvas's FIXED internal working level. The chain's absolute numbers — comp
# threshold -18, limiter -1.5, the -45 active floor — are all designed against this level.
# The dialog_db fader is applied at the FINAL level-set in remix, so chain behavior never
# changes with the fader position. (Bug this fixes: chunks used to be normalized straight
# to dialog_db, so a low fader like -42 silently switched the compressor OFF — TTS takes,
# which are far peakier than STS, then played with unshaved peaks and sounded louder.)


def dialogue_norm(chunk_audio, measure=None):
    """Normalize one placed chunk's active speech level to CHAIN_DB, the internal working level.
    STS returns arbitrary levels per call (a whisper once came back louder than the main line).
    `measure`: optional bool mask — measure ONLY those samples, apply the gain to the whole
    chunk. Used to keep a TTS take inserted INSIDE an STS chunk from dragging the surrounding
    STS off target (the take itself is re-normalized to the same target right after)."""
    src = chunk_audio[:, measure] if measure is not None and measure.any() else chunk_audio
    a = active_rms(src)
    if a <= 1e-8:
        return chunk_audio
    g = float(np.clip((10 ** (CHAIN_DB / 20.0)) / a, 10 ** (-18 / 20.0), 10 ** (18 / 20.0)))
    return chunk_audio * g


# ---------------------------------------------------------------- duck

def _close_runs(state, k):
    s = state.copy(); n = len(s); i = 0
    while i < n:
        if not s[i]:
            j = i
            while j < n and not s[j]:
                j += 1
            if 0 < i and j < n and (j - i) < k:
                s[i:j] = True
            i = j
        else:
            i += 1
    return s


def _open_runs(state, k):
    s = state.copy(); n = len(s); i = 0
    while i < n:
        if s[i]:
            j = i
            while j < n and s[j]:
                j += 1
            if (j - i) < k:
                s[i:j] = False
            i = j
        else:
            i += 1
    return s


def speech_state_gain(voice):
    """0..1 control that rises over whole UTTERANCES (not syllables): hysteresis gate + gap-merge +
    fast-attack/slow-release. Per-syllable ducking pumped several times a second; this moves ~once
    per utterance."""
    mono = np.mean(voice, axis=0) if voice.ndim == 2 else voice
    hop = int(SR * 0.01)
    nfr = max(1, len(mono) // hop)
    e = np.array([float(np.sqrt(np.mean(mono[i * hop:(i + 1) * hop] ** 2)) + 1e-9) for i in range(nfr)])
    edb = 20 * np.log10(e)
    valid = edb[edb > -80]
    peak = float(np.percentile(valid, 95)) if valid.size else -20.0
    hi, lo = peak - 26.0, peak - 36.0
    st = np.zeros(nfr, dtype=bool); on = False
    for i in range(nfr):
        if not on and edb[i] > hi:
            on = True
        elif on and edb[i] < lo:
            on = False
        st[i] = on
    st = _close_runs(st, 25)      # merge pauses < 250 ms inside one utterance
    st = _open_runs(st, 12)       # drop blips < 120 ms
    a_att, a_rel = 1 - np.exp(-10.0 / 30.0), 1 - np.exp(-10.0 / 400.0)
    g = np.zeros(nfr); acc = 0.0
    for i in range(nfr):
        tgt = 1.0 if st[i] else 0.0
        acc += (a_att if tgt > acc else a_rel) * (tgt - acc)
        g[i] = acc
    xs = np.arange(nfr) * hop + hop / 2
    return np.interp(np.arange(len(mono)), xs, g, left=g[0], right=g[-1]).astype(np.float32)


def _state_runs_seconds(state, n, thr=0.5):
    """Contiguous windows (seconds) where the duck state is engaged — for the timeline's amber lane."""
    mask = state[:n] > thr
    runs, i = [], 0
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if (j - i) > int(0.05 * SR):
                runs.append([round(i / SR, 2), round(j / SR, 2)])
            i = j
        else:
            i += 1
    return runs



def propose_duck_windows(base, orig_voice, exclude_windows=(), n=None):
    """The duck PROPOSER — replaces the removed invisible adaptive duck (user law: the engine
    never moves levels behind the scenes). Detects where a voice speaks (union of the new
    voice's and the original voice's activity, minus kept/cut windows) and returns the windows
    as seconds, for the caller to WRITE as ordinary visible duck bars the user owns."""
    state = np.maximum(speech_state_gain(base), speech_state_gain(orig_voice))
    n = min(base.shape[1], len(state)) if n is None else min(n, len(state))
    for (a, b) in exclude_windows:
        state[int(a * SR):min(len(state), int(b * SR))] = 0.0
    return _state_runs_seconds(state, n)


def bed_rumble(bed, s: Settings = None):
    """Low cut + optional boom shelf on the bed only — sub-rumble adds mud and eats headroom.
    Defaults reproduce the original fixed 40 Hz pass exactly. (No limiter on the bed, ever:
    a limiter on quiet material acts as a maximizer and un-does the duck — learned twice.)"""
    s = s or Settings()
    fx = [HighpassFilter(cutoff_frequency_hz=max(40.0, s.bed_hpf_hz))]
    if s.bed_bass_db:
        fx.append(LowShelfFilter(cutoff_frequency_hz=220, gain_db=s.bed_bass_db, q=0.7))
    return Pedalboard(fx)(bed, SR)


def bed_level(bed, s: Settings):
    """The background fader is TRUE in BOTH directions: set -16, the background MEASURES -16 —
    up or down. (The old down-only 'ceiling' left the background stranded 12+ dB under the
    voice with a dead up-knob; safe to amplify now that the original voice is its own layer —
    the bed is music/effects.) Safety: at most +24 dB of amplification, so a truly silent
    track can't become pure noise — the caller logs when that limit engages.
    Returns (bed, gain_db_applied, capped)."""
    total = 0.0
    capped = False
    for _ in range(3):   # the active-measurement set shifts with gain — iterate to the target
        a = active_rms(bed)
        if a <= 1e-8:
            break
        need_db = s.bed_db - 20.0 * np.log10(a)
        step = float(np.clip(need_db, -36.0 - total, 24.0 - total))
        capped = capped or (need_db > step + 0.01)
        if abs(step) < 0.3:
            break
        bed = bed * (10 ** (step / 20.0))
        total += step
    return bed, round(total, 1), capped


