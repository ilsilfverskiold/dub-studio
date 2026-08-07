"use strict";
const $ = id => document.getElementById(id);
const KINDNAMES = {speech:"words", singing:"singing", human_nonword:"laughter / vocal",
                   animal_vocal:"animal sound", non_speech:"SFX / music"};
const KINDTAG = {speech:"VO", singing:"SING", human_nonword:"vocal", animal_vocal:"animal", non_speech:"SFX"};
/* Ranges are deliberately WIDE — this is a learning desk. Push any fader to its end and you
   should clearly hear what it does (including hearing it ruined), then find the sweet spot. */
const FADERS = [
  ["balance","LEVELS — the number on the fader IS the measured loudness"],
  ["dialog_db",-50,-5,.5,"Voice loudness (dB)","The measured loudness of the new voice. Every STS and TTS line comes out at exactly this number, in every clip — the render log proves it each time."],
  ["bed_db",-60,-5,.5,"Background loudness (dB)","The measured loudness of the background — music, effects, room. SAME scale as Voice loudness: same number = equally loud. True in both directions — the log proves it each render."],
  ["duck_under_db",-60,-6,1,"Duck depth (dB)","How far a duck BAR dips everything under the new voice — only inside bars you can see on the timeline, back to normal at the bar's edge. At −60 the sound inside the bar is REMOVED entirely — true silence, ready for SFX or music you add later."],
  ["bedtone","BACKGROUND TONE"],
  ["bed_hpf_hz",40,400,5,"Background low cut","Cuts the background's lows below this frequency — the voice is untouched. At 40 Hz almost nothing changes; if the scene sounds muddy or bass-heavy, sweep up until the boom clears, then back off. Too far and the background turns into a phone speaker."],
  ["bed_bass_db",-18,6,.5,"Background boom (below ~220 Hz)","Low-end body of the background — basslines, room rumble, engine hum live here. Pull down to clean a boomy bed while keeping its warmth (gentler than the low cut); push up to hear exactly what 'boomy' means."],
  ["tone","VOICE TONE"],
  ["hpf_hz",40,400,5,"Rumble filter","Cuts everything below this frequency. At 40 Hz almost nothing changes; at 400 Hz the voice turns thin and telephone-like. Real dialogue chains sit around 80–120 Hz — sweep up until clean, then back off."],
  ["bass_shelf_db",-18,6,.5,"Boom (below ~220 Hz)","Low-end body of the voice. Push it up to hear what 'boomy' means, then cut until the mud is gone — stop before the voice goes thin."],
  ["notch_hz",200,4000,10,"Problem frequency — sweep","Play a line and sweep this slowly. At some point an ugly ring, honk or boxiness will jump out at you — that's the problem frequency. Park it there…"],
  ["notch_db",-24,0,.5,"…then cut it here","…and cut. At −24 that frequency is obliterated; around −6 to −10 is usually enough. Cut too deep and the voice sounds hollow and scooped."],
  ["presence_db",-12,12,.5,"Presence (~4 kHz)","The clarity zone. Push up and every consonant cuts through (crisp but fatiguing fast); pull down and the voice steps behind glass. Small moves go a long way."],
  ["air_db",0,12,.5,"Air (above ~9 kHz)","The 'expensive microphone' sheen. AI voices are often dull up here — push it and hear the top end open up. Too much turns hissy."],
  ["space","SPACE & DYNAMICS"],
  ["deess_db",0,18,.5,"De-esser","Softens harsh S and T sounds — only that band, only while it spikes. If sharp S's hurt on headphones, raise this until they don't. At 18 the voice starts to lisp."],
  ["comp_ratio",1,8,.1,"Compression","Evens out loud and quiet syllables. 1 = natural dynamics; 8 = squashed radio-ad voice. Listen to quiet word-endings come forward as you raise it — that's compression working."],
  ["room_wet",0,0.5,.01,"Room (ADR glue)","Puts the bone-dry AI voice inside a room. 0 = studio booth; 0.5 = obvious echo chamber. The dubbing trick: raise it until you just feel the space, then back off one notch."],
];
const STAGES = [
  {n:"01", label:"Detect",  title:"Detection",     hint:"find where speech sits before anything is generated"},
  {n:"02", label:"Cast",    title:"Cast & voices", hint:"who speaks each region, and with which voice"},
  {n:"03", label:"Convert", title:"Convert",       hint:"render the vocal layer — cached steps are skipped"},
  {n:"04", label:"Mix",     title:"Master mix",    hint:"all clips assembled — balance the layers, then render"},
];

let S = null, LOG = [], voicesList = null, vTarget = null, sigs = {}, ES = null;
let mix = {master:null, clip:{}};              // local fader edits before save
const UI = {view:"studio", stage:null, clip:null, region:null, mixScope:"master",
            rail:"activity", zoom:1, tlh:84, keyOpen:null, layout:"stack", split:0.46,
            projFilter:"all", takesCache:{}};

