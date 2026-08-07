"use strict";
/* ---------------- timeline dock ---------------- */
/* level lane: MEASURED loudness of the rendered audio — fetched per target, cached by mtime */
const LVL = {key:null, points:[], rendered:true, loading:null};
function ensureLevel(key, url){
  if (LVL.key === key || LVL.loading === key) return;
  LVL.loading = key;
  fetch(url).then(r=>r.json()).then(j=>{
    LVL.key = key; LVL.points = j.points||[]; LVL.rendered = j.rendered !== false; LVL.loading = null;
    sigs.tl = null; renderTimeline();
  }).catch(()=>{ LVL.loading = null; });
}
function lvlaneHTML(){
  if (!LVL.points.length) return "";
  return `<div class="lvlane${LVL.rendered?"":" src"}" title="${LVL.rendered
      ? "measured loudness of the rendered audio — flat speech tops = leveled dialogue"
      : "measured loudness of the ORIGINAL upload — converts into the leveled render"}">`
    + LVL.points.map(p => `<i style="height:${Math.max(1,(p+60)/60*40).toFixed(1)}%"></i>`).join("")
    + `</div>`;
}
function renderTimeline(){
  $("zoomlbl").textContent = (UI.zoom).toFixed(1) + "×";
  const isMix = (UI.stage ?? autoStage()) === 3;
  const c = cur();
  const tl = $("tl");
  tl.style.width = (UI.zoom * 100) + "%";
  tl.style.height = UI.tlh + "px";
  if (isMix && UI.mixScope === "master"){
    $("tlgutter").style.display = "none";   // no lanes on the master — the program gets full width
    // master program: clips back to back
    const durs = S.clips.map(x => x.duration || 1);
    const total = durs.reduce((a,b)=>a+b, 0) || 1;
    ensureLevel("master:" + S.out_mtime, "/api/level?master=1&v=" + S.out_mtime);
    const sig = JSON.stringify(["tlm", durs, UI.mixScope, UI.zoom, S.clips.map(x=>x.name),
                                LVL.key, LVL.points.length]);
    $("tlhint").textContent = "master program — clips play back to back · the amber floor is the MEASURED loudness: voice should peak the same height in every clip";
    $("tlacts").innerHTML = "";
    $("tdur").textContent = fmtT(total);
    if (sigs.tl === sig) return; sigs.tl = sig;
    let acc = 0, blocks = "", tags = "";
    S.clips.forEach((x, i) => {
      const l = acc/total*100, w = (x.duration||1)/total*100;
      const on = UI.mixScope === x.name;
      blocks += `<div class="blk${on?" on":""}" style="left:${l.toFixed(2)}%;width:${w.toFixed(2)}%;cursor:pointer"
        onpointerdown="event.stopPropagation()" onclick="event.stopPropagation();UI.mixScope='${x.name}';UI.clip='${x.name}';sigs={};renderAll()">
        ${waveHTML(Math.max(16, Math.round(60*w/100)), i+31, 34)}</div>`;
      tags += `<span class="btag${on?" on":""}" style="left:${l.toFixed(2)}%;width:${w.toFixed(2)}%">${x.name} · ${fmtT(x.duration)}</span>`;
      acc += x.duration || 1;
    });
    tl.innerHTML = `<div class="ticks">${ticksHTML(total)}</div>${lvlaneHTML()}
      <div class="lane">${blocks}${tags}</div><div class="needle" style="left:0"></div>`;
    return;
  }
  const nr = c ? (c.regions||[]).length : 0;
  const hasVoice = c && (c.chunks||[]).length;
  $("tlhint").textContent = !c ? "" :
    UI.region != null && nr > UI.region ? `region ${UI.region+1} selected — edit it in the panel above` :
    (nr ? "click a block to edit it · drag to move · edges to resize · double-click plays it"
        : "no voice regions yet — Detect finds them (free), or add one")
    + (hasVoice ? (c && !c.duck_owned && c.duck_windows_owned
        ? " — duck bars will be proposed on the next render (free): drawn on the timeline, yours to edit"
        : (UI.blade ? " — ✂ armed: click any bar to split it at that exact point · Esc to finish"
           : " — click selects · drag moves · edges resize · ✂ splits · remove is a button — render applies (free)")) : "");
  // action bar, organized: edit history │ what this stage edits │ the razor
  const stNow = UI.stage ?? autoStage();
  const sep = `<span class="tlsep"></span>`;
  const histG = `<button class="b sm" onclick="undoEdit()"${UNDO.length?"":" disabled"}>undo</button>
    <button class="b sm" onclick="redoEdit()"${REDO.length?"":" disabled"}>redo</button>`;
  const stageG = stNow < 3
    ? `<button class="b sm" onclick="addRegion()">+ add region</button>`
    : (hasVoice
        ? `<button class="b sm" onclick="addDuckRegion()">+ duck region</button>` +
          (c && c.duck_owned ? `<button class="b sm" onclick="resetDucking()"
            title="clear your duck bars and let the tool DRAW fresh ones where voices speak — visible, yours to edit">propose duck bars</button>` : "") +
          sep +
          `<button class="b sm" onclick="addBoostRegion()">+ boost region</button>` +
          sep +
          `<button class="b sm" title="turn the ORIGINAL voice down in a span you choose — any amount, click the bar to set it"
                   onclick="addDip()">+ voice turn-down</button>` +
          sep +
          `<button class="b sm" onclick="addTts()">+ add TTS</button>`
        : "");
  const bladeG = (stNow < 3 || hasVoice)
    ? `<button class="b sm${UI.blade ? " go" : ""}" title="split anything at the point you click"
        onclick="UI.blade=!UI.blade;sigs.tl=null;renderTimeline()">${UI.blade ? "✂ done" : "✂ cut"}</button>`
    : "";
  const selG = UI.selSeg
    ? `<button class="b sm" style="color:var(--err)" onclick="removeSelected()">remove selected</button>`
    : "";
  $("tlacts").innerHTML = c ? histG + (stageG ? sep + stageG : "") + (bladeG ? sep + bladeG : "")
                              + (selG ? sep + selG : "") : "";
  if (!c){ $("tlgutter").style.display = "none";
           tl.innerHTML = `<div class="ticks"></div><div class="lane"></div>`; sigs.tl = "empty"; return; }
  ensureLevel("clip:" + c.name + ":" + (c.final ? S.out_mtime : "src"),
              "/api/level?clip=" + encodeURIComponent(c.name) + "&v=" + S.out_mtime);
  const sig = JSON.stringify(["tlc", c.name, c.regions, c.assigns, UI.region, UI.zoom, c.duration,
                              c.duck_regions, c.duck_windows, c.duck_owned, c.duck_windows_owned,
                              c.chunks, c.voice_mutes, c.voice_splits, c.boost_regions, c.tts_takes, UI.blade, UI.selSeg,
                              c.preserves, c.keep_off, c.voice_dips, LVL.key, LVL.points.length]);
  const g = $("tlgutter");
  g.style.display = hasVoice ? "" : "none";   // the gutter exists only when there are lanes to name
  g.style.height = UI.tlh + "px";
  g.innerHTML = hasVoice ? `<span class="g1">added voice</span><span class="g2">bg −/+</span>` : "";
  if (sigs.tl === sig) return; sigs.tl = sig;
  const dur = c.duration || 1;
  let blocks = "", tags = "";
  const px = (a, b) => `left:${(a/dur*100).toFixed(2)}%;width:${Math.max(.4,(b-a)/dur*100).toFixed(2)}%`;
  const dimRef = isMix;                 // in Mix the region blocks are reference only — step back
  // the STS line is SCULPTABLE: cuts subtract from it (voice visibly shrinks), the razor
  // splits it into pieces anywhere, edge-drags trim, a click removes one piece
  tl.classList.toggle("blade", !!UI.blade);
  (c.chunks||[]).forEach(w => {
    let pieces = [[w[0], w[1]]];
    (c.voice_mutes||[]).forEach(m => {
      pieces = pieces.flatMap(p => {
        if (m[1] <= p[0] || m[0] >= p[1]) return [p];
        const out = [];
        if (m[0] > p[0] + 0.03) out.push([p[0], m[0]]);
        if (m[1] < p[1] - 0.03) out.push([m[1], p[1]]);
        return out;
      });
    });
    (c.voice_splits||[]).forEach(t => {
      pieces = pieces.flatMap(p =>
        (t > p[0] + 0.05 && t < p[1] - 0.05) ? [[p[0], t], [t, p[1]]] : [p]);
    });
    pieces.forEach(p => {
      const on = UI.selSeg && UI.selSeg.type === "vpiece" && UI.selSeg.ps === p[0] && UI.selSeg.pe === p[1];
      blocks += `<span class="vseg${on ? " on" : ""}" style="${px(p[0], p[1])}"
        title="STS voice ${p[0].toFixed(2)}–${p[1].toFixed(2)}s — ${UI.blade ? "click to SPLIT here" : "click selects · drag edges: in trims, out restores"}"
        onpointerdown="vsegDown(event,${p[0]},${p[1]},${w[0]},${w[1]})"></span>`;
    });
  });
  (c.voice_splits||[]).forEach(t => {
    blocks += `<span class="splitmark" style="left:${(t/dur*100).toFixed(2)}%"></span>`;
  });
  // the REMOVED-VOICE strip: where the user cut the new voice, the striped line IS the undo —
  // click it and the new voice comes back there. Never decoration, always a control.
  (c.voice_mutes||[]).forEach(m => {
    blocks += `<span class="oseg" style="${px(m[0], m[1])}"
      onclick="event.stopPropagation();restoreCut(${m[0]},${m[1]})"
      onpointerdown="event.stopPropagation()"
      title="removed voice ${m[0].toFixed(2)}–${m[1].toFixed(2)}s — the original plays here · CLICK to bring the new voice back"></span>`;
  });
  // ORIGINAL-VOICE turn-downs: every dip of the original-voice layer is a visible bar — the
  // auto ones (under new-voice lines) and yours alike. Click one to adjust its amount or remove.
  (c.voice_dips||[]).forEach((dd, i) => {
    const on = UI.selSeg && UI.selSeg.type === "dip" && UI.selSeg.i === i;
    blocks += `<span class="dipseg${on?" on":""}" style="${px(dd.start, dd.end)}"
      onpointerdown="dipDown(event,${i})"
      title="original voice turned down ${Math.round(dd.db ?? 60)} dB (${dd.start.toFixed(2)}–${dd.end.toFixed(2)}s) — click to adjust · drag moves · edges resize"></span>`;
  });
  (c.tts_takes||[]).forEach((t, i) => {
    const stale = t.has && t.voice_stale;   // the take no longer matches voice/text — amber
    blocks += `<span class="tseg${t.has ? "" : " pend"}${stale ? " stale" : ""}${UI.tts === i ? " on" : ""}" data-i="${i}"
      style="${px(t.start, t.end)}"
      title="TTS ${t.start}–${t.end}s${t.character ? " · @" + t.character : ""}${
        stale ? " — made with a PREVIOUS voice or text: open it and Regenerate"
              : t.has ? " — generated" : " — not generated yet"} · drag to place · edges resize · click opens its editor"
      onpointerdown="tsegDown(event,${i})"></span>`;
  });
  // ONE class of duck: every ducking space on screen is the same editable amber block —
  // the detector's spaces and yours are interchangeable. Touch any of them and the map is yours.
  duckList(c).forEach((r, i) => {
    const on = UI.selSeg && UI.selSeg.type === "duck" && UI.selSeg.i === i;
    blocks += `<span class="duckseg${on ? " on" : ""}" data-i="${i}" style="${px(r.start, r.end)}"
      title="duck bar ${r.start}–${r.end}s · −${Math.round(Math.abs(r.db ?? (S.settings||{}).duck_under_db ?? 28))} dB — click to adjust the amount · drag moves · edges resize"
      onpointerdown="duckDown(event,${i})"></span>`;
  });
  (c.boost_regions||[]).forEach((r, i) => {
    const on = UI.selSeg && UI.selSeg.type === "boost" && UI.selSeg.i === i;
    blocks += `<span class="boostseg${on ? " on" : ""}" data-i="${i}" style="${px(r.start, r.end)}"
      title="boost bar ${r.start}–${r.end}s · +${Math.round(Math.abs(r.db ?? 6))} dB — click to adjust the amount · drag moves · edges resize"
      onpointerdown="boostDown(event,${i})"></span>`;
  });
  (c.regions||[]).forEach((r, i) => {
    const l = r.start/dur*100, w = Math.max(0.8, (r.end-r.start)/dur*100);
    const a = (c.assigns||[])[i] || {};
    const nsp = a.kind && a.kind !== "speech";
    const on = UI.region === i;
    const tag = (KINDTAG[a.kind] || "VO") + " · " + (r.end-r.start).toFixed(2) + "s";
    blocks += `<div class="blk${on?" on":""}${nsp?" nsp":""}${dimRef?" dim":""}" data-i="${i}"
      style="left:${l.toFixed(2)}%;width:${w.toFixed(2)}%"
      onpointerdown="rgDown(event,${i})" ondblclick="playRegion(${r.start},${r.end})"
      title="${a.character||"—"} · ${r.start}–${r.end}s">
      ${waveHTML(Math.max(8, Math.round(26*w/100*UI.zoom)), i+3, 34)}</div>`;
    tags += `<span class="btag${on?" on":""}" style="left:${l.toFixed(2)}%;width:${Math.max(w,4).toFixed(2)}%">${tag}</span>`;
  });
  tl.innerHTML = `<div class="ticks">${ticksHTML(dur)}</div>${lvlaneHTML()}
    <div class="lane">${blocks}${tags}</div><div class="needle" style="left:0"></div>`;
  $("tdur").textContent = fmtT(dur);
}
function ticksHTML(dur){
  const step = dur * UI.zoom > 24 ? 2 : 1;
  let h = "";
  for (let s = 0; s <= Math.floor(dur); s += step){
    const l = s/dur*100;
    h += `<span style="left:${l.toFixed(2)}%;transform:translateX(${s===0?"0":"-50%"});
      color:${(s/step)%2 ? "#3a3a44" : "#55555f"}">${fmtT(s)}</span>`;
  }
  return h;
}
function tlSeek(e){
  if (e.target.closest(".blk")) return;
  if (UI.selSeg){ UI.selSeg = null; sigs.tl = null; renderTimeline(); }
  const v = $("vid"); const tl = $("tl");
  if (!v || !tl) return;
  const r = tl.getBoundingClientRect();
  const frac = (e.clientX - r.left) / r.width;
  // the timeline's length must match what the MONITOR is playing: on the master program the
  // reel spans all clips — seeking with one clip's duration always landed inside clip 1.
  const isMix = (UI.stage ?? autoStage()) === 3;
  const span = (isMix && UI.mixScope === "master")
      ? S.clips.reduce((a,x)=>a+(x.duration||1), 0)
      : (cur()||{}).duration;
  if (!span) return;
  v.currentTime = Math.max(0, Math.min(span, frac * span));
  v.play();
}
/* THE duck bars — one list, always editable, each with its own amount. "unowned" exists only
   for the moment between pressing "propose duck bars" and the render that draws them. */
function duckList(c){
  if (c.duck_owned) return c.duck_regions || [];
  // between the propose click and its render: show the last render's map as a preview
  if (c.duck_windows_owned) return [];
  return (c.duck_windows || []).map(w => ({start: w[0], end: w[1]}));
}
function saveDucks(c, regions){
  pushUndo(c);
  c.duck_regions = regions;
  c.duck_owned = true;
  sigs.tl = null; renderTimeline();
  post("/api/duck", {clip: c.name, regions, owned: true});
}
function resetDucking(){
  const c = cur(); if (!c) return;
  pushUndo(c);
  c.duck_regions = []; c.duck_owned = false;
  sigs.tl = null; renderTimeline();
  post("/api/duck", {clip: c.name, regions: [], owned: false});
}
function saveBoosts(c, regions){
  pushUndo(c);
  c.boost_regions = regions;
  sigs.tl = null; renderTimeline();
  post("/api/duck", {clip: c.name, boost_regions: regions});
}
function addBoostRegion(){
  const c = cur(); if (!c) return;
  const at = addAt(c);
  saveBoosts(c, [...(c.boost_regions||[]), {start:+Math.max(0,at).toFixed(2),
                                            end:+Math.min(c.duration||1, at+1).toFixed(2)}]);
}
function removeBoostRegion(i){
  const c = cur(); if (!c) return;
  saveBoosts(c, (c.boost_regions||[]).filter((_,j)=> j!==i));
}
let dgB = null;
function boostDown(e, i){
  e.preventDefault(); e.stopPropagation();
  const c = cur(); if (!c || !(c.boost_regions||[])[i]) return;
  const el = e.currentTarget, rect = el.getBoundingClientRect(), edge = 6;
  const mode = (e.clientX - rect.left < edge) ? "l" : (rect.right - e.clientX < edge) ? "r" : "m";
  dgB = {i, mode, el, dur: c.duration || 1, startX: e.clientX, r0: {...c.boost_regions[i]}, moved:false};
  el.setPointerCapture(e.pointerId);
  el.onpointermove = boostMove; el.onpointerup = boostUp;
}
function boostMove(e){
  if (!dgB) return;
  const tl = $("tl"); if (!tl) return;
  const ds = (e.clientX - dgB.startX) / tl.getBoundingClientRect().width * dgB.dur;
  if (Math.abs(e.clientX - dgB.startX) > 3) dgB.moved = true;
  let s = dgB.r0.start, en = dgB.r0.end;
  if (dgB.mode === "m"){ s += ds; en += ds; }
  else if (dgB.mode === "l"){ s = Math.min(en - .1, s + ds); }
  else { en = Math.max(s + .1, en + ds); }
  s = Math.max(0, Math.min(s, dgB.dur - .1)); en = Math.max(s + .1, Math.min(en, dgB.dur));
  dgB.cur = {start:+s.toFixed(2), end:+en.toFixed(2)};
  dgB.el.style.left = (s/dgB.dur*100)+"%";
  dgB.el.style.width = ((en-s)/dgB.dur*100)+"%";
}
function boostUp(e){
  const d = dgB; dgB = null;
  if (!d) return;
  d.el.onpointermove = d.el.onpointerup = null;
  const c = cur(); if (!c) return;
  if (!d.moved){
    if (UI.blade){
      const tlr = $("tl").getBoundingClientRect();
      const t = +((e.clientX - tlr.left) / tlr.width * (c.duration||1)).toFixed(2);
      const r = c.boost_regions[d.i];
      if (r && t > r.start + 0.05 && t < r.end - 0.05){
        const list = c.boost_regions.slice();
        list.splice(d.i, 1, {start:r.start, end:t}, {start:t, end:r.end});
        saveBoosts(c, list);
      }
      return;
    }
    selectSeg("boost", d.i);
    return;
  }
  if (!d.cur) return;
  saveBoosts(c, c.boost_regions.map((r,j)=> j===d.i ? {...r, ...d.cur} : r));
}
let dgD = null;
/* original-voice turn-down bars: same gestures as duck bars — drag moves, edges resize,
   click selects (amount editor), ✂ splits. Fully the user's, auto ones included. */
let dgP = null;
function dipDown(e, i){
  e.preventDefault(); e.stopPropagation();
  const c = cur(); if (!c || !(c.voice_dips||[])[i]) return;
  const el = e.currentTarget, rect = el.getBoundingClientRect(), edge = 6;
  const mode = (e.clientX - rect.left < edge) ? "l" : (rect.right - e.clientX < edge) ? "r" : "m";
  dgP = {i, mode, el, dur: c.duration || 1, startX: e.clientX, r0: {...c.voice_dips[i]}, moved:false};
  el.setPointerCapture(e.pointerId);
  el.onpointermove = dipMove; el.onpointerup = dipUp;
}
function dipMove(e){
  if (!dgP) return;
  const tl = $("tl"); if (!tl) return;
  const ds = (e.clientX - dgP.startX) / tl.getBoundingClientRect().width * dgP.dur;
  if (Math.abs(e.clientX - dgP.startX) > 3) dgP.moved = true;
  let s = dgP.r0.start, en = dgP.r0.end;
  if (dgP.mode === "m"){ s += ds; en += ds; }
  else if (dgP.mode === "l"){ s = Math.min(en - .1, s + ds); }
  else { en = Math.max(s + .1, en + ds); }
  s = Math.max(0, Math.min(s, dgP.dur - .1)); en = Math.max(s + .1, Math.min(en, dgP.dur));
  dgP.cur = {start:+s.toFixed(2), end:+en.toFixed(2)};
  dgP.el.style.left = (s/dgP.dur*100)+"%";
  dgP.el.style.width = ((en-s)/dgP.dur*100)+"%";
}
function dipUp(e){
  const d = dgP; dgP = null;
  if (!d) return;
  d.el.onpointermove = d.el.onpointerup = null;
  const c = cur(); if (!c) return;
  if (!d.moved){ selectSeg("dip", d.i); return; }
  if (!d.cur) return;
  saveDips(c, (c.voice_dips||[]).map((r,j)=> j===d.i ? {...r, ...d.cur} : r));
}
function addAt(c){
  // where a new bar lands: the playhead if it's genuinely inside THIS clip and not at the
  // edge — otherwise mid-clip, always visible (the master player's time is NOT clip time)
  const v = $("vid");
  const dur = c.duration || 1;
  const isMaster = UI.mixScope === "master" && (UI.stage ?? autoStage()) === 3;
  let at = (!isMaster && v) ? v.currentTime : NaN;
  if (!(at >= 0) || at > dur - 0.6) at = Math.max(0, dur / 2 - 0.5);
  return +at.toFixed(2);
}
function addDip(){
  const c = cur(); if (!c) return;
  const at = addAt(c);
  saveDips(c, [...(c.voice_dips||[]), {start:+Math.max(0,at).toFixed(2),
                                       end:+Math.min(c.duration||1, at+1).toFixed(2), db:60}]);
}
function duckDown(e, i){
  e.preventDefault(); e.stopPropagation();
  const c = cur(); if (!c || !duckList(c)[i]) return;
  const el = e.currentTarget, rect = el.getBoundingClientRect(), edge = 6;
  const mode = (e.clientX - rect.left < edge) ? "l" : (rect.right - e.clientX < edge) ? "r" : "m";
  dgD = {i, mode, el, dur: c.duration || 1, startX: e.clientX, r0: {...duckList(c)[i]}, moved:false};
  el.setPointerCapture(e.pointerId);
  el.onpointermove = duckMove; el.onpointerup = duckUp;
}
function duckMove(e){
  if (!dgD) return;
  const tl = $("tl"); if (!tl) return;
  const ds = (e.clientX - dgD.startX) / tl.getBoundingClientRect().width * dgD.dur;
  if (Math.abs(e.clientX - dgD.startX) > 3) dgD.moved = true;
  let s = dgD.r0.start, en = dgD.r0.end;
  if (dgD.mode === "m"){ s += ds; en += ds; }
  else if (dgD.mode === "l"){ s = Math.min(en - .1, s + ds); }
  else { en = Math.max(s + .1, en + ds); }
  s = Math.max(0, Math.min(s, dgD.dur - .1)); en = Math.max(s + .1, Math.min(en, dgD.dur));
  dgD.cur = {start:+s.toFixed(2), end:+en.toFixed(2)};
  dgD.el.style.left = (s/dgD.dur*100)+"%";
  dgD.el.style.width = ((en-s)/dgD.dur*100)+"%";
}
function duckUp(e){
  const d = dgD; dgD = null;
  if (!d) return;
  d.el.onpointermove = d.el.onpointerup = null;
  const c = cur(); if (!c) return;
  if (!d.moved){
    if (UI.blade){                                   // razor: split this duck space in two
      const tlr = $("tl").getBoundingClientRect();
      const t = +((e.clientX - tlr.left) / tlr.width * (c.duration||1)).toFixed(2);
      const r = duckList(c)[d.i];
      if (r && t > r.start + 0.05 && t < r.end - 0.05){
        const list = duckList(c).slice();
        list.splice(d.i, 1, {start:r.start, end:t}, {start:t, end:r.end});
        saveDucks(c, list);
      }
      return;
    }
    selectSeg("duck", d.i);                          // click SELECTS — removal is a button
    return;
  }
  if (!d.cur) return;
  saveDucks(c, duckList(c).map((r,j)=> j===d.i ? {...r, ...d.cur} : r));
}
function addDuckRegion(){
  const c = cur(); if (!c) return;
  const at = addAt(c);
  saveDucks(c, [...duckList(c), {start:+Math.max(0,at).toFixed(2),
                                 end:+Math.min(c.duration||1, at+1).toFixed(2)}]);
}
function removeDuckRegion(i){
  const c = cur(); if (!c) return;
  saveDucks(c, duckList(c).filter((_,j)=> j!==i));
}
/* undo/redo for every timeline edit — the regret button. Snapshots the clip's editable state
   before each change; restoring only posts the fields that actually differ, so undoing a duck
   edit never touches regions (and never invalidates a paid conversion by accident). */
const UNDO = [], REDO = [];
function snapClip(c){
  return JSON.parse(JSON.stringify({clip: c.name, regions: c.regions||[], voices: c.voices,
    assigns: c.assigns||[], duck_regions: c.duck_regions||[], duck_owned: !!c.duck_owned, boost_regions: c.boost_regions||[], tts_takes: c.tts_takes||[],
    voice_mutes: c.voice_mutes||[], voice_splits: c.voice_splits||[]}));
}
function pushUndo(c){
  if (!c) return;
  UNDO.push(snapClip(c));
  if (UNDO.length > 50) UNDO.shift();
  REDO.length = 0;
}
function applySnap(s){
  const c = S.clips.find(x => x.name === s.clip);
  if (!c) return;
  UI.clip = s.clip;
  const J = JSON.stringify;
  if (J(s.regions) !== J(c.regions||[]) || s.voices !== c.voices)
    post("/api/regions", {clip: s.clip, regions: s.regions, voices: s.voices});
  if (J(s.assigns) !== J(c.assigns||[]) && s.assigns.length)
    post("/api/assign", {clip: s.clip, assigns: s.assigns});
  if (J(s.duck_regions) !== J(c.duck_regions||[]) || s.duck_owned !== !!c.duck_owned ||
      J(s.voice_mutes) !== J(c.voice_mutes||[]) || J(s.voice_splits) !== J(c.voice_splits||[]) ||
      J(s.boost_regions) !== J(c.boost_regions||[]) || J(s.tts_takes) !== J(c.tts_takes||[]))
    post("/api/duck", {clip: s.clip, regions: s.duck_regions, owned: s.duck_owned,
                       voice_mutes: s.voice_mutes, voice_splits: s.voice_splits,
                       boost_regions: s.boost_regions, tts_takes: s.tts_takes});
  Object.assign(c, {regions: s.regions, voices: s.voices, assigns: s.assigns,
                    duck_regions: s.duck_regions, duck_owned: s.duck_owned,
                    voice_mutes: s.voice_mutes, voice_splits: s.voice_splits,
                    boost_regions: s.boost_regions, tts_takes: s.tts_takes});
  sigs = {}; renderAll();
}
function undoEdit(){
  const s = UNDO.pop();
  if (!s){ toast("nothing to undo", "info"); return; }
  const c = S.clips.find(x => x.name === s.clip);
  if (c) REDO.push(snapClip(c));
  applySnap(s);
}
function redoEdit(){
  const s = REDO.pop();
  if (!s){ toast("nothing to redo", "info"); return; }
  const c = S.clips.find(x => x.name === s.clip);
  if (c) UNDO.push(snapClip(c));
  applySnap(s);
}

/* TTS windows: placed and sized ON THE TIMELINE; transcription reads the original vocals
   underneath the window; generation speaks the corrected words in the character's voice */
function saveTts(c, takes){
  pushUndo(c);
  c.tts_takes = takes;
  sigs.tl = null; sigs.panel = null; renderTimeline(); renderPanel();
  post("/api/duck", {clip: c.name, tts_takes: takes});
}
function addTts(){
  const c = cur(); if (!c) return;
  const at = addAt(c);
  const takes = [...(c.tts_takes||[]), {start:+Math.max(0,at).toFixed(2),
                                        end:+Math.min(c.duration||1, at+1).toFixed(2),
                                        text:"", file:"", has:false}];
  saveTts(c, takes);
  openTtsModal(takes.length - 1);
}
function removeTts(i){
  const c = cur(); if (!c) return;
  saveTts(c, (c.tts_takes||[]).filter((_,j)=> j!==i));
}
function restoreCut(a, b){
  // the striped removed-voice line was clicked: bring the new voice back in that window
  const c = cur(); if (!c) return;
  pushUndo(c);
  c.voice_mutes = (c.voice_mutes||[]).filter(m => !(Math.abs(m[0]-a) < 0.01 && Math.abs(m[1]-b) < 0.01));
  sigs.tl = null; renderTimeline();
  post("/api/duck", {clip:c.name, voice_mutes:c.voice_mutes});
}
let dgT = null;
function tsegDown(e, i){
  e.preventDefault(); e.stopPropagation();
  const c = cur(); if (!c || !(c.tts_takes||[])[i]) return;
  const el = e.currentTarget, rect = el.getBoundingClientRect(), edge = 6;
  const mode = (e.clientX - rect.left < edge) ? "l" : (rect.right - e.clientX < edge) ? "r" : "m";
  const t = c.tts_takes[i];
  dgT = {i, mode, el, dur: c.duration || 1, startX: e.clientX, r0: {start: t.start, end: t.end}, moved:false};
  el.setPointerCapture(e.pointerId);
  el.onpointermove = tsegMove; el.onpointerup = tsegUp;
}
function tsegMove(e){
  if (!dgT) return;
  const tl = $("tl"); if (!tl) return;
  const ds = (e.clientX - dgT.startX) / tl.getBoundingClientRect().width * dgT.dur;
  if (Math.abs(e.clientX - dgT.startX) > 3) dgT.moved = true;
  let s = dgT.r0.start, en = dgT.r0.end;
  if (dgT.mode === "m"){ s += ds; en += ds; }
  else if (dgT.mode === "l"){ s = Math.min(en - .2, s + ds); }
  else { en = Math.max(s + .2, en + ds); }
  s = Math.max(0, Math.min(s, dgT.dur - .2)); en = Math.max(s + .2, Math.min(en, dgT.dur));
  dgT.cur = {start:+s.toFixed(2), end:+en.toFixed(2)};
  dgT.el.style.left = (s/dgT.dur*100)+"%";
  dgT.el.style.width = ((en-s)/dgT.dur*100)+"%";
}
function tsegUp(e){
  const d = dgT; dgT = null;
  if (!d) return;
  d.el.onpointermove = d.el.onpointerup = null;
  const c = cur(); if (!c) return;
  if (!d.moved){ openTtsModal(d.i); return; }       // click opens the window's editor
  if (!d.cur) return;
  // the speech-start point MOVES WITH the bar — dragging the window is dragging the audio
  const takes = c.tts_takes.map((t,j)=> j===d.i
      ? {...t, start:d.cur.start, end:d.cur.end,
         place_at: t.place_at != null ? +(t.place_at + (d.cur.start - t.start)).toFixed(2) : t.place_at}
      : t);
  saveTts(c, takes);
}
function ttCharOptions(c, sel){
  // any character introduced ANYWHERE in the project speaks here — plus the door to a new one
  $("tt_char").innerHTML = allChars(c).map(ch =>
    `<option value="${ch.identifier}" ${ch.identifier===sel?"selected":""}>@${ch.identifier} — ${voiceName(ch.voice_id)}</option>`).join("")
    + `<option value="__new__">+ new character…</option>`;
  // Cancel restores the last REAL selection. Tracked HERE because programmatic selection
  // (modal open, a successful add) fires no onchange event.
  if (sel && sel !== "__new__") UI.ttPrevChar = sel;
}
function ttNewCharRow(show){
  const row = $("tt_newchar");
  row.style.display = show ? "flex" : "none";
  if (!show) return;
  $("tt_nc_note").textContent = "Added to the whole project — usable in every clip, for TTS " +
    "and casting alike. Adding a character never re-converts anything.";
  $("tt_nc_play").innerHTML = PLAY;
  $("tt_nc_name").value = "";
  ensureVoices().then(() => {
    $("tt_nc_voice").innerHTML = (voicesList||[]).map(v =>
      `<option value="${v.voice_id}">${esc(v.name)}${Object.values(v.labels||{}).length
        ? " — " + esc(Object.values(v.labels).slice(0,2).join(", ")) : ""}</option>`).join("")
      || `<option value="">no voices on the account</option>`;
  });
  setTimeout(() => $("tt_nc_name").focus(), 30);
}
$("tt_nc_play").onclick = () => {
  const v = (voicesList||[]).find(x => x.voice_id === $("tt_nc_voice").value);
  if (v && v.preview){ $("preview").src = v.preview; $("preview").play(); }
  else toast("no preview available for this voice");
};
$("tt_nc_x").onclick = () => {
  ttNewCharRow(false);
  const c = cur();
  if (c) ttCharOptions(c, UI.ttPrevChar || "");
};
$("tt_nc_add").onclick = async () => {
  const c = cur(); if (!c) return;
  const id = ($("tt_nc_name").value || "").trim().replace(/^@+/, "").replace(/\s+/g, "_");
  if (!id){ $("tt_nc_note").textContent = "give the character a name first"; return; }
  const r = await fetch("/api/char", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"add", identifier:id,
                            voice_id: $("tt_nc_voice").value || undefined})});
  const j = await r.json().catch(()=>({}));
  if (!r.ok){ $("tt_nc_note").textContent = j.error || "could not add the character"; return; }
  // show the new character IMMEDIATELY from what we know — never wait on a status poll
  // that may have started before the add landed; refresh() reconciles with disk after
  if (!(S.cast || []).some(x => x.identifier === id))
    (S.cast = S.cast || []).push({identifier: id, voice_id: $("tt_nc_voice").value || ""});
  ttNewCharRow(false);
  const cc = cur();
  if (cc) ttCharOptions(cc, id);     // selected — Generate speaks this window as them
  refresh();
  toast(`@${id} added to the project cast — this window speaks as them now`, "info");
};
function openTtsModal(i){
  const c = cur(); if (!c || !(c.tts_takes||[])[i]) return;
  const t = c.tts_takes[i];
  UI.tts = i;
  sigs.tl = null; renderTimeline();                 // selection ring on the segment
  $("tt_span").textContent = `${t.start.toFixed(2)}–${t.end.toFixed(2)}s`;
  // ONE short line; amber only when something needs the user's attention
  $("tt_note").textContent = t.has && t.voice_stale
    ? "This take was made with a PREVIOUS voice or text — Generate re-makes it (price shown first); until then the old take plays."
    : t.has
    ? "Plays your generated line instead of the STS — edit the words and regenerate, or remove the window to bring the STS back."
    : "Speaks your text in the character's voice instead of the STS — type the words, or let \"Get the words\" read them off the lips.";
  $("tt_note").style.color = (t.has && t.voice_stale) ? "var(--amber)" : "";
  $("tt_text").value = t.text || "";
  const mid = (t.start + t.end) / 2;
  let autoChar = ((c.chars||[])[0]||{}).identifier || "";
  (c.regions||[]).forEach((r, ri) => {
    if (r.start <= mid && mid <= r.end && (c.assigns||[])[ri] && c.assigns[ri].character)
      autoChar = c.assigns[ri].character;
  });
  const selChar = t.character || autoChar;
  ttCharOptions(c, selChar);
  ttNewCharRow(false);
  // "spoken by → + new character…" expands the inline row below — name it, pick and audition
  // its voice, Add. One modal, no stacking. Adding cast NEVER re-converts anything.
  $("tt_char").onchange = e => {
    if (e.target.value !== "__new__"){ UI.ttPrevChar = e.target.value; ttNewCharRow(false); return; }
    ttNewCharRow(true);
  };
  $("tt_speed").value = t.speed || "";
  $("tt_place").value = (t.place_at != null ? t.place_at : t.start).toFixed(2);
  $("tt_gen").textContent = t.has ? "Regenerate TTS" : "Generate TTS";
  const lp = $("tt_listen");
  if (lp) lp.innerHTML = t.has && c.solo_voice
    ? `<button class="lchip" onclick="playSoloSpan('${c.solo_voice}', ${t.start}, ${t.end})">▶ voice only</button>
       <button class="lchip" onclick="playRegion(${t.start}, ${t.end})">▶ in the mix</button>`
    : "";
  $("ttsbg").classList.add("show");
}
function closeTtsModal(discard){
  // whatever is in the fields PERSISTS — typed words and tweaked delivery are work, never
  // discarded. EXCEPT when the window itself is being removed: saving on the way out would
  // race the removal save and could resurrect the window (the two-clicks-to-remove bug).
  const c = cur();
  if (!discard && c && UI.tts != null && (c.tts_takes||[])[UI.tts]){
    const t = c.tts_takes[UI.tts];
    const text = $("tt_text").value;
    const speed = parseFloat($("tt_speed").value) || undefined;
    const place = parseFloat($("tt_place").value);
    // "__new__" is the add-row sentinel, never a character — closing mid-add keeps the old one
    const raw = $("tt_char").value;
    const chosen = (raw && raw !== "__new__" ? raw : t.character);
    if (text !== (t.text || "") || speed !== t.speed || chosen !== t.character ||
        (!isNaN(place) && place !== (t.place_at != null ? t.place_at : t.start))){
      const takes = c.tts_takes.map((x,k)=> k===UI.tts
        ? {...x, text, speed, character: chosen,
           place_at: isNaN(place) ? x.place_at : +place.toFixed(2)} : x);
      c.tts_takes = takes;
      post("/api/duck", {clip:c.name, tts_takes:takes});
    }
  }
  $("ttsbg").classList.remove("show");
  UI.tts = null;
  sigs.tl = null; renderTimeline();
}
// () wrapper is LOAD-BEARING: assigning closeTtsModal directly passed the CLICK EVENT as
// `discard` (truthy) — the Close button silently threw away every field edit, forever
$("tt_close").onclick = () => closeTtsModal();
$("tt_rm").onclick = () => { const i = UI.tts; closeTtsModal(true); if (i != null) removeTts(i); };
$("ttsbg").onclick = e => { if (e.target === $("ttsbg")) closeTtsModal(); };

function ttsBusy(msg){
  const on = !!msg;
  $("tt_status").style.display = on ? "flex" : "none";
  $("tt_status_txt").textContent = msg || "";
  for (const id of ["tt_tr","tt_gen","tt_rm","tt_close"]) $(id).disabled = on;
  $("tt_text").readOnly = on;
}
async function transcribeWindow(){
  const c = cur(); if (!c || UI.tts == null) return;
  const t = (c.tts_takes||[])[UI.tts]; if (!t) return;
  const secs = t.end - t.start;
  if (!await ask({title:"Transcribe this window", cost:"< $0.01",
                  detail:`Gemini listens to ${secs.toFixed(1)}s of the original vocals under the window and writes down the words — billed per audio token, a fraction of a cent at this length. Correct the words before generating.`,
                  action:"Transcribe"})) return;
  ttsBusy("Gemini is reading the window — exact words, timing and pace. Takes a few seconds; nothing to do but wait.");
  let j;
  try {
    const r = await fetch("/api/transcribe", {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({clip:c.name, start:t.start, end:t.end, approved:true})});
    j = await r.json();
    if (!r.ok){ toast(j.error || "transcription failed"); return; }
  } finally {
    ttsBusy(null);
  }
  $("tt_text").value = j.text || "";
  $("tt_speed").value = j.speed;            // shown, editable — this is the learning surface
  $("tt_place").value = j.place_at.toFixed(2);
  const takes = c.tts_takes.map((x,k)=> k===UI.tts ? {...x, speed:j.speed, place_at:j.place_at} : x);
  c.tts_takes = takes;
  post("/api/duck", {clip:c.name, tts_takes:takes});
}
async function generateTts(){
  const c = cur(); if (!c || UI.tts == null) return;
  const text = ($("tt_text") ? $("tt_text").value : "").trim();
  if (!text){ toast("no text — nothing to speak"); return; }
  if ($("tt_char").value === "__new__"){
    toast("finish adding the character first (or pick one from the list)"); return;
  }
  const usd = Math.max(0.01, text.length * 0.0002);
  if (!await ask({title:"Generate the line", cost:`≈ $${usd.toFixed(2)}`,
                  detail:`${text.length} characters = ${text.length} ElevenLabs credits (1 credit per character, exact), spoken in the character's voice for this window. The $ figure assumes ≈$0.20 per 1000 credits — plans vary. Cached: the same text again is free.`,
                  action:"Generate"})) return;
  ttsBusy("ElevenLabs is performing the line in the character's voice — a few seconds.");
  try {
    const r = await fetch("/api/tts-retake", {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({clip:c.name, index:UI.tts, text, approved:true,
                              character: $("tt_char").value || undefined,
                              speed: parseFloat($("tt_speed").value) || undefined,
                              place_at: parseFloat($("tt_place").value)})});
    const j = await r.json().catch(()=>({}));
    if (!r.ok){ toast(j.error || "TTS failed"); return; }
  } finally {
    ttsBusy(null);
  }
  // sync the local window BEFORE closing — the close-persist must never clobber the fresh take
  const tk = (c.tts_takes||[])[UI.tts];
  if (tk){
    tk.text = text;
    tk.speed = parseFloat($("tt_speed").value) || tk.speed;
    const pl_ = parseFloat($("tt_place").value);
    if (!isNaN(pl_)) tk.place_at = +pl_.toFixed(2);
    tk.character = $("tt_char").value || tk.character;
    tk.has = true;
  }
  closeTtsModal();
  toast("line generated — the clip is rendering now (free); press play when the rendering… chip clears", "info");
  refresh();
}

function mergeCuts(list){
  const s = list.map(m => [Math.min(m[0], m[1]), Math.max(m[0], m[1])]).sort((a,b)=>a[0]-b[0]);
  const out = [];
  for (const m of s){
    if (out.length && m[0] <= out[out.length-1][1] + 0.02)
      out[out.length-1][1] = Math.max(out[out.length-1][1], m[1]);
    else out.push([...m]);
  }
  return out.map(m => [+m[0].toFixed(2), +m[1].toFixed(2)]);
}
function saveCuts(c, mutes){
  pushUndo(c);
  c.voice_mutes = mergeCuts(mutes);
  sigs.tl = null; renderTimeline();
  post("/api/duck", {clip:c.name, voice_mutes: c.voice_mutes});
}
/* the STS bar is a LAYER: drag an edge inward to trim it away, drag it back OUT to restore it
   (up to the take's real extent). A cut leaves nothing behind — just absence. */
function subtractSpan(mutes, a, b){
  return mutes.flatMap(m => {
    if (b <= m[0] || a >= m[1]) return [m];
    const out = [];
    if (a > m[0] + 0.02) out.push([m[0], a]);
    if (b < m[1] - 0.02) out.push([b, m[1]]);
    return out;
  });
}
let dgV = null;
function vsegDown(e, ps, pe, w0, w1){
  e.preventDefault(); e.stopPropagation();
  const c = cur(); if (!c) return;
  const el = e.currentTarget, rect = el.getBoundingClientRect(), edge = 6;
  const mode = (e.clientX - rect.left < edge) ? "l" : (rect.right - e.clientX < edge) ? "r" : "m";
  dgV = {ps, pe, w0, w1, mode, el, dur: c.duration || 1, startX: e.clientX, moved:false, cur:null};
  el.setPointerCapture(e.pointerId);
  el.onpointermove = vsegMove; el.onpointerup = vsegUp;
}
function vsegMove(e){
  if (!dgV || dgV.mode === "m") return;
  const tl = $("tl"); if (!tl) return;
  const ds = (e.clientX - dgV.startX) / tl.getBoundingClientRect().width * dgV.dur;
  if (Math.abs(e.clientX - dgV.startX) > 3) dgV.moved = true;
  if (dgV.mode === "l"){
    // inward = trim; outward = restore, up to the take's own start
    const s = Math.max(dgV.w0, Math.min(dgV.pe - 0.05, dgV.ps + ds));
    dgV.cur = s;
    dgV.el.style.left = (s/dgV.dur*100)+"%";
    dgV.el.style.width = ((dgV.pe-s)/dgV.dur*100)+"%";
  } else {
    const en = Math.min(dgV.w1, Math.max(dgV.ps + 0.05, dgV.pe + ds));
    dgV.cur = en;
    dgV.el.style.width = ((en-dgV.ps)/dgV.dur*100)+"%";
  }
}
function vsegUp(e){
  const d = dgV; dgV = null;
  if (!d) return;
  d.el.onpointermove = d.el.onpointerup = null;
  const c = cur(); if (!c) return;
  if (d.mode !== "m" && d.moved && d.cur != null){
    let mutes = (c.voice_mutes||[]).slice();
    if (d.mode === "l"){
      if (d.cur > d.ps + 0.02) mutes = [...mutes, [d.ps, d.cur]];        // trimmed in -> cut
      else if (d.cur < d.ps - 0.02) mutes = subtractSpan(mutes, d.cur, d.ps);  // pulled out -> restore
    } else {
      if (d.cur < d.pe - 0.02) mutes = [...mutes, [d.cur, d.pe]];
      else if (d.cur > d.pe + 0.02) mutes = subtractSpan(mutes, d.pe, d.cur);
    }
    saveCuts(c, mutes);
    return;
  }
  if (d.moved) return;
  if (UI.blade){
    // razor: split the piece exactly where the blade clicked — no audio changes, just a seam
    const tlr = $("tl").getBoundingClientRect();
    const t = +((e.clientX - tlr.left) / tlr.width * d.dur).toFixed(2);
    if (t > d.ps + 0.05 && t < d.pe - 0.05){
      pushUndo(c);
      c.voice_splits = [...new Set([...(c.voice_splits||[]), t])].sort((a,b)=>a-b);
      sigs.tl = null; renderTimeline();
      post("/api/duck", {clip:c.name, voice_splits: c.voice_splits});
    }
    return;
  }
  selectSeg("vpiece", null, d.ps, d.pe);              // click SELECTS — removal is a button
}
function selectSeg(type, i, ps, pe){
  const cur_ = UI.selSeg;
  const same = cur_ && cur_.type === type &&
    (type === "vpiece" ? (cur_.ps === ps && cur_.pe === pe) : cur_.i === i);
  UI.selSeg = same ? null : {type, i, ps, pe};
  sigs.tl = null; renderTimeline();
  sigs.panel = null; renderPanel();     // the bar's amount editor must appear INSTANTLY
}
function saveDips(c, rows){
  pushUndo(c);
  c.voice_dips = rows;
  sigs.tl = null; sigs.panel = null; renderTimeline(); renderPanel();
  post("/api/duck", {clip:c.name, voice_dips: rows});
}
function saveBarAmount(){
  const c = cur(); const s = UI.selSeg;
  if (!c || !s) return;
  const v = Math.abs(parseFloat($("bar_db").value) || 0);
  if (!v){ toast("enter an amount in dB"); return; }
  if (s.type === "duck")      saveDucks(c, duckList(c).map((r,j)=> j===s.i ? {...r, db:v} : r));
  else if (s.type === "boost") saveBoosts(c, (c.boost_regions||[]).map((r,j)=> j===s.i ? {...r, db:v} : r));
  else if (s.type === "dip")   saveDips(c, (c.voice_dips||[]).map((r,j)=> j===s.i ? {...r, db:v} : r));
}
function removeSelected(){
  const c = cur(); const s = UI.selSeg;
  if (!c || !s) return;
  UI.selSeg = null;
  if (s.type === "duck") saveDucks(c, duckList(c).filter((_,j)=> j!==s.i));
  else if (s.type === "boost") saveBoosts(c, (c.boost_regions||[]).filter((_,j)=> j!==s.i));
  else if (s.type === "dip") saveDips(c, (c.voice_dips||[]).filter((_,j)=> j!==s.i));
  else if (s.type === "vpiece"){
    saveCuts(c, [...(c.voice_mutes||[]), [s.ps, s.pe]]);
    toast(`STS voice removed ${s.ps.toFixed(2)}–${s.pe.toFixed(2)}s — the original takes over · drag a neighbouring edge out (or undo) to restore`, "info");
  }
}

/* drag / resize / click-select on region blocks */
let drag = null;
function rgDown(e, i){
  if ((UI.stage ?? autoStage()) === 3) return;
  e.preventDefault(); e.stopPropagation();
  const c = cur(); if (!c) return;
  const el = e.currentTarget, rect = el.getBoundingClientRect(), edge = 7;
  const mode = (e.clientX - rect.left < edge) ? "l" : (rect.right - e.clientX < edge) ? "r" : "m";
  drag = {i, mode, el, dur: c.duration, startX: e.clientX, r0: {...c.regions[i]}, moved:false};
  el.setPointerCapture(e.pointerId);
  el.onpointermove = rgMove; el.onpointerup = rgUp;
}
function rgMove(e){
  if (!drag) return;
  const tl = $("tl"); if (!tl) return;
  const ds = (e.clientX - drag.startX) / tl.getBoundingClientRect().width * drag.dur;
  if (Math.abs(e.clientX - drag.startX) > 3) drag.moved = true;
  let s = drag.r0.start, en = drag.r0.end;
  if (drag.mode === "m"){ s += ds; en += ds; }
  else if (drag.mode === "l"){ s = Math.min(en - .1, s + ds); }
  else { en = Math.max(s + .1, en + ds); }
  s = Math.max(0, Math.min(s, drag.dur - .1)); en = Math.max(s + .1, Math.min(en, drag.dur));
  drag.cur = {start:+s.toFixed(2), end:+en.toFixed(2)};
  drag.el.style.left = (s/drag.dur*100)+"%";
  drag.el.style.width = ((en-s)/drag.dur*100)+"%";
  const rs = $("ri_s"), re = $("ri_e");
  if (rs && UI.region === drag.i){ rs.value = drag.cur.start; re.value = drag.cur.end; }
}
function rgUp(e){
  const d = drag; drag = null;
  if (!d) return;
  d.el.onpointermove = d.el.onpointerup = null;
  if (d.moved && d.cur){
    const c = cur();
    pushUndo(c);
    const regions = c.regions.map((r,j)=> j===d.i ? d.cur : r);
    post("/api/regions", {clip:c.name, regions, voices:c.voices});
  } else if (UI.blade){
    // razor on a detection region: split it in two; the assignment is inherited by both halves
    const c = cur();
    const tlr = $("tl").getBoundingClientRect();
    const t = +((e.clientX - tlr.left) / tlr.width * (c.duration||1)).toFixed(2);
    const r = c.regions[d.i];
    if (r && t > r.start + 0.05 && t < r.end - 0.05){
      pushUndo(c);
      const regions = c.regions.slice();
      regions.splice(d.i, 1, {start:r.start, end:t}, {start:t, end:r.end});
      const assigns = (c.assigns||[]).slice();
      if (assigns[d.i]) assigns.splice(d.i, 0, {...assigns[d.i]});
      post("/api/regions", {clip:c.name, regions, voices:c.voices})
        .then(()=> assigns.length && post("/api/assign", {clip:c.name, assigns}));
    }
  } else {
    UI.region = (UI.region === d.i) ? null : d.i;
    sigs = {}; renderAll();
  }
}

