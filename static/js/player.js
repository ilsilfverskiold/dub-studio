"use strict";
/* ---------------- player ---------------- */
function renderPlayer(){
  const narrow = window.innerWidth < 880;
  const side = !narrow && UI.layout === "side";
  const ws = $("wsplit");
  const pl = $("player"), dv = $("splitdiv");
  if (narrow){
    // phone/tablet: the player is a full-width block on top — no split, no docking
    ws.style.flexDirection = "column";
    pl.style.width = "100%";
    pl.style.height = "38vh";
    dv.style.display = "none";
  } else {
    dv.style.display = "";
  }
  ws.style.flexDirection = side ? "row" : "column";
  if (side){
    pl.style.width = `min(${(UI.split*100).toFixed(1)}%, calc(100% - 314px))`;
    pl.style.height = "100%";
    dv.style.width = "14px"; dv.style.height = "100%"; dv.style.cursor = "col-resize";
    dv.firstElementChild.style.cssText = "width:3px;height:46px;border-radius:3px;background:rgba(255,255,255,.12)";
  } else if (!narrow){
    pl.style.width = "100%";
    pl.style.height = (UI.split*100).toFixed(1) + "%";
    dv.style.width = "100%"; dv.style.height = "14px"; dv.style.cursor = "row-resize";
    dv.firstElementChild.style.cssText = "width:46px;height:3px;border-radius:3px;background:rgba(255,255,255,.12)";
  }
  const isMix = (UI.stage ?? autoStage()) === 3;
  const c = cur();
  let src = null, title = "no clip", badge = "", dl = null;
  if (isMix && UI.mixScope === "master" && S.reel){
    src = S.reel + "?v=" + S.out_mtime; title = `Master program · ${S.clips.length} clips`; badge = "mix";
    dl = S.reel;                                  // a rendered result — downloadable
  } else if (c){
    title = c.name; badge = STAGES[UI.stage ?? autoStage()].label.toLowerCase();
    const showFinal = c.final && !c.stale;
    src = showFinal ? c.final + "?v=" + S.out_mtime : c.src;
    if (showFinal) dl = c.final;                  // only finished takes download — not the source
  }
  const sig = JSON.stringify(["pl", src, title, badge, dl]);
  if (sigs.player !== sig){
    sigs.player = sig;
    pl.innerHTML = src ? `
      <video id="vid" src="${src}" preload="metadata"></video>
      <div class="titlebar" onpointerdown="dockDown(event)">
        <div class="grip"><i></i><i></i><i></i><i></i><i></i><i></i></div>
        <span class="ttl">${esc(title)}</span><span class="badge">${badge}</span>
        <span class="badge" id="rendering" style="background:rgba(240,185,85,.14);
          border-color:rgba(240,185,85,.3);color:var(--amber);display:none">rendering…</span>
        <span class="tlab" id="ptime">0:00</span>
        ${dl ? `<a class="icb" href="${dl}" download aria-label="download what's playing"
                 title="download what's playing" style="color:#d4d4dc"
                 onpointerdown="event.stopPropagation()">↓</a>` : ""}
      </div>
      <div class="drophint" id="drophint"><span id="droplabel"></span></div>`
      : `<div class="empty" style="border:none">Drop clips into the strip above to start.<br>
         Detection is free — anything that costs money asks first, with the price.</div>`;
    wireVideo();
  }
}
function wireVideo(){
  const v = $("vid");
  if (!v || v._wired) return;
  v._wired = true;
  // place the player to fit the video's real shape: portrait docks sideways, landscape on top.
  // A manual drag wins until the next clip is selected.
  v.addEventListener("loadedmetadata", () => {
    if (UI.layoutManual || !v.videoWidth) return;
    const want = v.videoHeight > v.videoWidth ? "side" : "stack";
    if (UI.layout !== want){ UI.layout = want; renderPlayer(); }
  });
  v.addEventListener("timeupdate", () => {
    const d = v.duration || (cur()||{}).duration || 1;
    const n = document.querySelector("#tl .needle");
    if (n) n.style.left = (v.currentTime / d * 100) + "%";
    const pt = $("ptime"); if (pt) pt.textContent = fmtT(v.currentTime) + " / " + fmtT(d);
    $("tcur").textContent = fmtT(v.currentTime);
    $("tdur").textContent = fmtT(d);
  });
  v.addEventListener("play", () => $("bigplay").innerHTML = PAUSE);
  v.addEventListener("pause", () => $("bigplay").innerHTML = PLAY);
  v.addEventListener("click", () => togglePlay());
}
function togglePlay(){ const v = $("vid"); if (v) v.paused ? v.play() : v.pause(); }
/* dock-drag: right = side layout, down = stacked — straight from the mock */
function dockDown(e){
  if (e.target.closest("button")) return;
  e.preventDefault();
  const x0 = e.clientX, y0 = e.clientY;
  const hint = $("drophint"), lbl = $("droplabel");
  const move = ev => {
    const dx = ev.clientX - x0, dy = ev.clientY - y0;
    if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
    hint.style.display = "flex";
    lbl.textContent = dx > 90 ? "dock right" : dy > 90 ? "dock top — full width" :
                      "drag right to dock sideways, down for full width";
  };
  const up = ev => {
    window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
    if (hint) hint.style.display = "none";
    const dx = ev.clientX - x0, dy = ev.clientY - y0;
    if (Math.abs(dx) > Math.abs(dy) && dx > 90) UI.layout = "side";
    else if (Math.abs(dy) > Math.abs(dx) && dy > 90) UI.layout = "stack";
    else return;
    UI.layoutManual = true;              // the user placed it — stop auto-placing this clip
    sigs.player = null; renderPlayer();
  };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
}
$("splitdiv").onpointerdown = e => {
  e.preventDefault();
  const ws = $("wsplit").getBoundingClientRect();
  const side = UI.layout === "side";
  const move = ev => {
    const p = side ? (ev.clientX - ws.left) / ws.width : (ev.clientY - ws.top) / ws.height;
    UI.split = Math.min(side ? Math.max(0.25,(ws.width-320)/ws.width) : 0.72, Math.max(0.22, p));
    renderPlayer();
  };
  const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
};
/* drag the dock's top edge down to slim the timeline — the workspace gets the room */
$("dockgrip").onpointerdown = e => {
  e.preventDefault();
  const y0 = e.clientY, h0 = UI.tlh;
  const move = ev => {
    UI.tlh = Math.max(48, Math.min(140, h0 - (ev.clientY - y0)));
    $("tl").style.height = UI.tlh + "px";
    $("tlgutter").style.height = UI.tlh + "px";
  };
  const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
};

