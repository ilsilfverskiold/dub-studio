"use strict";
/* ---------------- sidebar: nav + pipeline + keys ---------------- */
function stageOf(c){ return {uploaded:0, detected:1, cast:2, converted:3}[c.stage] ?? 0; }
function autoStage(){
  const c = cur();
  if (!c) return 0;
  return stageOf(c);
}
function renderSidebar(){
  $("nav_studio").classList.toggle("on", UI.view==="studio");
  $("nav_projects").classList.toggle("on", UI.view==="projects");
  $("projcnt").textContent = (S.projects||[]).length || "";
  // the pipeline belongs to the studio — it means nothing on the Projects page
  $("pipe").style.display = UI.view === "projects" ? "none" : "flex";
  const cs = UI.stage ?? autoStage();
  const stages = S.clips.map(stageOf);
  let html = `<div class="pipehead"><span class="eyebrow" style="padding:0">PIPELINE</span>
    <span class="prog">${S.clips.length ? Math.min(...stages)+1 : 0}/4</span></div>`;
  STAGES.forEach((s, i) => {
    const done = S.clips.length && stages.every(x => x > i);
    const chip = done ? ["done","done"] : (i===cs ? ["active","active"] : ["", "ready"]);
    html += `<button class="stagerow${i===cs?" on":""}${done?" done":""}" onclick="UI.stage=${i};UI.view='studio';renderAll()">
      <span class="bar"></span><span class="num">${s.n}</span><span class="lbl">${s.label}</span>
      <span class="chip ${chip[0]}">${chip[1]}</span></button>`;
  });
  const sig = html;
  if (sigs.pipe !== sig){ sigs.pipe = sig; $("pipe").innerHTML = html; }
  // keys
  const nset = ["elevenlabs","gemini","huggingface"].filter(n => S.keys[n]).length;
  $("keysum").textContent = nset + "/3";
  $("keysum").style.color = nset === 3 ? "var(--ok)" : "var(--amber)";
  const ksig = JSON.stringify([S.keys, UI.keyOpen]);
  if (sigs.keys !== ksig){
    sigs.keys = ksig;
    // presence only — no fragment of a key is ever shown (or even sent to this page)
    $("keyrows").innerHTML = ["elevenlabs","gemini","huggingface"].map(n => {
      const set = S.keys[n];
      return `<div class="keyrow${UI.keyOpen===n?" open":""}">
        <button class="head" title="${set ? "connected — click to replace" : "missing — click to add"}"
                onclick="UI.keyOpen = UI.keyOpen==='${n}' ? null : '${n}'; sigs.keys=null; renderSidebar()">
          <span class="dot" style="background:${set?"var(--ok)":"var(--amber)"}"></span>
          <span class="nm">${n}</span>
        </button>
        ${UI.keyOpen===n ? `<div style="display:flex;gap:5px">
          <input type="password" id="key_${n}" placeholder="paste key" autocomplete="off">
          <button class="save" onclick="saveKey('${n}')">Save</button></div>` : ""}
      </div>`;
    }).join("");
  }
}
async function saveKey(n){
  const v = $("key_"+n).value.trim();
  if (!v) { UI.keyOpen = null; sigs.keys = null; renderSidebar(); return; }
  await fetch("/api/keys", {method:"POST", headers:{"Content-Type":"application/json"},
                            body: JSON.stringify({[n]: v})});
  UI.keyOpen = null; sigs.keys = null;
  refresh();
}

