"use strict";
/* ---------------- clip strip ---------------- */
function renderStrip(){
  const isMix = (UI.stage ?? autoStage()) === 3;
  const list = S.clips;
  const sig = JSON.stringify([list.map(c=>[c.name,c.stage,(c.regions||[]).length]), UI.clip, UI.mixScope, isMix, S.master, S.reel]);
  if (sigs.strip === sig) return; sigs.strip = sig;
  let html = list.map((c, i) => {
    const on = isMix ? UI.mixScope === c.name : UI.clip === c.name;
    const done = stageOf(c);
    return `<div class="ccard${on?" on":""}" onclick="selectClip('${c.name}')">
      <div class="thumb"></div>
      <div style="display:flex;flex-direction:column;gap:4px">
        <span class="nm">${c.name}</span>
        <div style="display:flex;align-items:center;gap:7px">
          <span class="meta">${fmtT(c.duration)} · ${(c.regions||[]).length} region${(c.regions||[]).length===1?"":"s"}</span>
          <span class="dots">${[0,1,2,3].map(d=>`<i style="background:${d<=done?"var(--acc)":"#26262f"}"></i>`).join("")}</span>
        </div>
      </div>
      <span class="rowic">
        <button class="icb" aria-label="move ${c.name} earlier" title="play earlier in the reel"
                onclick="event.stopPropagation();moveClip(${i},-1)" ${i===0?"disabled":""}>←</button>
        <button class="icb" aria-label="move ${c.name} later" title="play later in the reel"
                onclick="event.stopPropagation();moveClip(${i},1)" ${i===list.length-1?"disabled":""}>→</button>
        <button class="icb x" aria-label="remove ${c.name}" title="remove clip"
                onclick="event.stopPropagation();removeClip('${c.name}')">✕</button>
      </span>
    </div>`;
  }).join("");
  const anyConverted = S.clips.some(x => x.stage === "converted" || x.final);
  if (isMix && anyConverted){
    const m = S.master, when = m.rendered_at ? new Date(m.rendered_at*1000).toTimeString().slice(0,5) : null;
    const stateTxt = m.state==="none" ? "nothing converted yet" :
      m.state==="stale" ? (when ? `rendered ${when} — changes since` : "not rendered yet") : `rendered ${when} — current`;
    html += `<div class="msep"><i></i><b>▸</b></div>
      <div class="mastercard${UI.mixScope==="master"?" on":""}" onclick="UI.mixScope='master';UI.region=null;sigs={};renderAll()">
        <div class="mstack"><i></i><i></i><i></i></div>
        <div style="display:flex;flex-direction:column;gap:4px">
          <div style="display:flex;align-items:center;gap:7px">
            <span style="font-size:12.5px;font-weight:600;color:${UI.mixScope==="master"?"var(--text)":"var(--text2)"}">Master</span>
            <span class="mchip">ASSEMBLED</span></div>
          <span class="mono" style="font-size:10px;color:${m.state==="ok"?"var(--ok)":"var(--dim)"}">${stateTxt}</span>
        </div>
        <div class="vline"></div>
        ${m.state==="ok" && (S.reel || S.clips.some(c=>c.final))
          ? `<span class="b sm go" style="display:inline-flex;align-items:center" onclick="event.stopPropagation();downloadMaster()">Download</span>`
          : `<span class="b sm" style="display:inline-flex;align-items:center" onclick="event.stopPropagation();post('/api/render-master')">Render</span>`}
      </div>`;
  } else {
    html += `<button class="b dashed" id="addclips" style="height:48px" onclick="$('file').click()">+ add clips — or drop them anywhere</button>`;
  }
  $("strip").innerHTML = html;
}
function selectClip(name){
  const isMix = (UI.stage ?? autoStage()) === 3;
  UI.clip = name; UI.region = null; UI.tts = null; UI.selSeg = null;
  // stay on the stage the user is working in — switching clips never yanks them elsewhere.
  // (With no stage chosen yet, the panel still follows each clip's own progress.)
  UI.layoutManual = false;               // new clip -> let it place itself by its shape again
  if (isMix) UI.mixScope = name;
  sigs = {}; renderAll();
}
function moveClip(i, d){
  const names = S.clips.map(c=>c.name);
  const [n] = names.splice(i,1); names.splice(i+d,0,n);
  post("/api/reorder", {clips:names});
}
async function removeClip(name){
  if (await ask({title:`Remove ${name}?`, tone:"danger",
                 detail:"Removes the clip from this project. Its processed state is content-addressed — re-uploading the same file revives it free.",
                 note:"", action:"Remove clip"}))
    post("/api/remove", {clip:name});
}
async function upload(files){
  if (!files || !files.length) return;
  const btn = $("addclips");
  for (const f of files){
    if (btn) btn.textContent = `uploading ${f.name}…`;
    const r = await fetch("/api/upload", {method:"POST", headers:{"X-Filename":f.name}, body:f});
    if (!r.ok){ const e = await r.json().catch(()=>({error:"upload failed"})); toast(`${f.name}: ${e.error}`); }
  }
  sigs.strip = null;
  refresh();
}
/* the file input lives OUTSIDE the re-rendered strip so an open picker can never be destroyed */
$("file").onchange = e => { upload(e.target.files); e.target.value = ""; };
/* drop clips anywhere in the studio — without these handlers the browser just opens the file */
document.addEventListener("dragover", e => {
  e.preventDefault();
  if (UI.view === "studio") $("strip").style.boxShadow = "inset 0 0 0 1.5px rgba(122,129,221,.6)";
});
document.addEventListener("dragleave", e => {
  if (!e.relatedTarget) $("strip").style.boxShadow = "";
});
document.addEventListener("drop", e => {
  e.preventDefault();
  $("strip").style.boxShadow = "";
  if (UI.view === "studio" && e.dataTransfer.files.length) upload(e.dataTransfer.files);
});

