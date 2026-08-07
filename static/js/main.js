"use strict";
/* ---------------- session maintenance ---------------- */
$("reset").onclick = async () => {
  if (await ask({title:"Clear this project?", tone:"danger",
                 detail:"Deletes this project's clips, results, takes and cast. Other projects are untouched. Its caches are kept, so re-processing the same clips stays free.",
                 note:"", action:"Clear project"}))
    post("/api/reset").then(()=>{ LOG=[]; UI.clip=null; UI.region=null; sigs={}; });
};
$("clearcache").onclick = async () => {
  if (await ask({title:"Clear this project's caches?", tone:"danger",
                 detail:"Every isolation, conversion and separation this project has paid for is forgotten. Voice choices are kept. The next run is a full run and will be billed again.",
                 note:"", action:"Clear caches"}))
    post("/api/clear-cache");
};

/* ---------------- SSE ---------------- */
function reconnectSSE(){
  // every reconnect is a project switch, and the new stream replays that project's history —
  // start from an empty console so two projects' logs never sit in the same list
  if (ES){ ES.close(); ES = null; }
  LOG = []; sigs.rail = null;
  sse();
}
function sse(){
  ES = new EventSource("/api/events");
  ES.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.stage === "project" && ev.message === "switched"){ reconnectSSE(); refresh(); return; }
    LOG.push(ev);
    if (LOG.length > 400) LOG = LOG.slice(-300);
    if (UI.rail === "activity"){ sigs.rail = null; renderRail(); }
    if (ev.level === "error"){ UI.rail = "activity"; sigs.rail = null; renderRail(); }
    if (["done","ok","error","metric","warn"].includes(ev.level)) refresh();
  };
}

/* ---------------- main loop ---------------- */
function renderAll(){
  if (!S) return;
  renderHeader(); renderSidebar(); renderProjects();
  if (UI.view === "studio"){ renderStrip(); renderPlayer(); renderPanel(); renderRail(); renderTimeline(); }
}
async function refresh(){
  let s;
  try { s = await (await fetch("/api/status")).json(); }
  catch(e){ $("busy").classList.remove("hidden"); return; }
  S = s;
  if (s.page_mtime){   // silent auto-reload when a newer build of this page exists
    if (!window._pageV) window._pageV = s.page_mtime;
    else if (window._pageV !== s.page_mtime){ location.reload(); return; }
  }
  if (UI.pendingOpen && !s.busy){
    const id = UI.pendingOpen; UI.pendingOpen = null;
    openProject(id);
    return;
  }
  if (UI.wantPath){                    // the URL the page LOADED on named a project — open it
    const p = projectFromPath(UI.wantPath); UI.wantPath = null;
    if (p && !p.active){ openProject(p.id, false); return; }
  }
  if (!UI.clip && s.clips.length) UI.clip = s.clips[0].name;
  if (UI.clip && !s.clips.find(c=>c.name===UI.clip)){
    UI.clip = s.clips.length ? s.clips[0].name : null; UI.region = null;
  }
  if (UI.mixScope !== "master" && !s.clips.find(c=>c.name===UI.mixScope)) UI.mixScope = "master";
  ensureVoices().catch(()=>{});
  renderAll();
}
/* collapsible right rail — collapsed, the whole workspace widens */
function setRail(open){
  UI.railOpen = open;
  try { localStorage.railOpen = open ? "1" : "0"; } catch(e){}
  document.querySelector("aside.rail").classList.toggle("closed", !open);
}
$("railhide").onclick = () => setRail(false);
$("railshow").onclick = () => setRail(true);
try {
  if (localStorage.railOpen === "0") setRail(false);
  else if (localStorage.railOpen === undefined && window.innerWidth < 1100) setRail(false);
} catch(e){}
let _rsz = null;
window.addEventListener("resize", () => {
  clearTimeout(_rsz);
  _rsz = setTimeout(() => { sigs.player = null; renderPlayer(); renderTimeline(); }, 150);
});

document.addEventListener("keydown", e => {
  const typing = ["INPUT","SELECT","TEXTAREA"].includes(document.activeElement.tagName);
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z" && !typing){
    e.preventDefault();
    e.shiftKey ? redoEdit() : undoEdit();
    return;
  }
  if (e.key === "Escape" && UI.blade){ UI.blade = false; sigs.tl = null; renderTimeline(); }
  else if (e.key === "Escape" && UI.selSeg){ UI.selSeg = null; sigs.tl = null; renderTimeline(); }
  if (e.key === " " && !["INPUT","SELECT","TEXTAREA"].includes(document.activeElement.tagName)){
    e.preventDefault(); togglePlay();
  }
});
$("bigplay").innerHTML = PLAY;
sse(); refresh(); setInterval(refresh, 4000);
