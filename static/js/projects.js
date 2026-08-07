"use strict";
/* ---------------- projects view ---------------- */
function renderProjects(){
  $("projview").classList.toggle("hidden", UI.view!=="projects");
  $("studioview").classList.toggle("hidden", UI.view==="projects");
  $("dock").classList.toggle("hidden", UI.view==="projects");
  if (UI.view !== "projects") return;
  const list = (S.projects||[]).filter(p => {
    if (UI.projFilter === "active") return !p.reel_rendered;
    if (UI.projFilter === "done") return p.reel_rendered;
    return true;
  });
  const sig = JSON.stringify([list, UI.projFilter, S.project]);
  if (sigs.proj === sig) return; sigs.proj = sig;
  const filters = [["all","All"],["active","Active"],["done","Delivered"]].map(([k,l]) =>
    `<button class="pf${UI.projFilter===k?" on":""}" onclick="UI.projFilter='${k}';sigs.proj=null;renderProjects()">${l}</button>`).join("");
  $("projview").innerHTML = `
    <div class="projhead">
      <div><h1>Projects</h1>
        <div class="sub">${(S.projects||[]).length} project(s) — each is fully self-contained: its own clips, cast, caches and master</div></div>
      <div class="projfilters">${filters}
        <button class="pf" style="background:var(--acc);border:none;color:#fff;font-weight:600"
                onclick="newProject()">+ New project</button></div>
    </div>
    <div class="projgrid">` + list.map(p => `
      <div class="pcard${p.active?" active":""}" onclick="openProject('${p.id}')">
        <div class="thumbs"><i></i><i></i><i></i></div>
        <div class="titlerow"><span class="nm">${esc(p.name)}</span>
          ${p.active ? '<span class="chip active">open</span>' :
            p.reel_rendered ? '<span class="chip done">delivered</span>' : '<span class="chip">draft</span>'}</div>
        <div class="metarow"><span class="meta">${p.clips} clip${p.clips===1?"":"s"}</span>
          <span class="dots">${[0,1,2,3].map(d=>`<i style="background:${d < (p.reel_rendered?4:p.clips?2:1) ? "var(--acc)" : "#26262f"}"></i>`).join("")}</span></div>
        <div class="foot"><span class="when">${new Date(p.created*1000).toLocaleDateString()}</span>
          <span style="margin-left:auto;display:flex;gap:2px">
            <button class="icb" aria-label="rename project" title="rename"
                    onclick="event.stopPropagation();renameProject('${p.id}', '${esc(p.name)}')">✎</button>
            <button class="icb x" aria-label="delete project" title="delete"
                    onclick="event.stopPropagation();deleteProject('${p.id}', '${esc(p.name)}')">✕</button></span></div>
      </div>`).join("") + `</div>`;
}
async function newProject(){
  const name = await askText({title:"New project", value:"Project " + ((S.projects||[]).length + 1),
                              note:"Fully self-contained: its own clips, cast, caches and master.",
                              action:"Create project"});
  if (name === null || !name.trim()) return;
  await post("/api/projects", {action:"create", name});
  UI.view = "studio"; UI.clip = null; UI.stage = null; sigs = {}; reconnectSSE();
  refresh();
}
async function openProject(id, push=true){
  const p = (S.projects||[]).find(x=>x.id===id);
  if (p && p.active){ nav("studio", push); return; }
  // ALWAYS attempt — the client's busy flag can be 4s stale; only the server's answer counts
  const r = await fetch("/api/projects", {method:"POST", headers:{"Content-Type":"application/json"},
                                          body: JSON.stringify({action:"open", id})});
  if (!r.ok){
    UI.pendingOpen = id;
    toast(`opening "${p ? p.name : "project"}" as soon as the current render finishes…`, "info");
    return;
  }
  UI.clip = null; UI.stage = null; UI.region = null; UI.mixScope = "master";
  nav("studio", push);
  mix = {master:null, clip:{}}; UI.takesCache = {}; sigs = {}; reconnectSSE();
  refresh();
}
async function renameProject(id, old){
  const name = await askText({title:"Rename project", value:old, action:"Rename"});
  if (name === null || !name.trim() || name === old) return;
  await post("/api/projects", {action:"rename", id, name});
}
async function deleteProject(id, name){
  if (await ask({title:`Delete "${name}"?`, tone:"danger",
                 detail:"Deletes the project's clips, results, cast and caches from disk. Other projects are untouched.",
                 note:"", action:"Delete project"})){
    await post("/api/projects", {action:"delete", id});
    sigs = {}; reconnectSSE(); refresh();
  }
}

