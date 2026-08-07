"use strict";
/* ---------------- header: usage + smart CTA ---------------- */
function ctaState(){
  if (!S || !S.clips.length) return {label:"Add clips to start", disabled:true};
  const nDet = S.clips.filter(c=>c.stage==="uploaded").length;
  if (nDet) return {label:`Detect ${nDet} clip${nDet>1?"s":""} · free`, run: () => post("/api/analyze")};
  const cast = S.clips.filter(c=>c.stage==="detected");
  if (cast.length) return {label:`Cast ${cast.length} clip${cast.length>1?"s":""} ≈$${(cast.length*0.02).toFixed(2)}`,
    run: async () => {
      if (await ask({title:"Identify the cast", cost:`≈ $${(cast.length*0.02).toFixed(2)}`,
                     detail:cast.map(c=>c.name).join("  ·  "),
                     action:`Cast ${cast.length} clip${cast.length>1?"s":""}`}))
        post("/api/identify", {approved:true});
    }};
  const conv = S.clips.filter(c=>c.stage!=="converted");
  if (conv.length){
    const el = conv.reduce((a,c)=>a+(c.est.el_usd||0),0);
    return {label:`Convert ${conv.length} clip${conv.length>1?"s":""} ≈$${el.toFixed(2)}`, run: async () => {
      const cr = conv.reduce((a,c)=>a+(c.est.credits||0),0);
      if (await ask({title:"Convert with ElevenLabs", cost:`≈ $${el.toFixed(2)}`,
                     detail:`${conv.map(c=>c.name).join("  ·  ")} — ≈ ${cr} credits on your key; cached steps are skipped and shown live.`,
                     action:`Convert ${conv.length} clip${conv.length>1?"s":""}`}))
        post("/api/convert", {approved:true});
    }};
  }
  if (S.master.state !== "ok") return {label:"Render master · free", run: () => post("/api/render-master")};
  return {label:"Master is current", disabled:true};
}
function renderHeader(){
  // always show WHICH project this studio is — the chip and the tab title both carry the name
  const pname = (S.project && S.project.name) || "…";
  $("projname").textContent = pname;
  document.title = pname + " — Dub Studio";
  // the URL always names what's on screen: "/" becomes /p/<name>, renames and project
  // switches re-stamp it — replace, never push, so history stays the user's clicks
  const want = pathFor(UI.view);
  if (location.pathname !== want) history.replaceState({view: UI.view}, "", want);
  const st = ctaState();
  $("cta").style.display = UI.view === "projects" ? "none" : "";   // a clip action — meaningless on the Projects page
  $("projchip").style.display = UI.view === "projects" ? "none" : "";  // the page itself shows all projects
  $("cta").textContent = st.label;
  $("cta").disabled = !!st.disabled || S.busy;
  $("busy").classList.toggle("hidden", !S.busy);
  const rd = $("rendering");
  if (rd) rd.style.display = S.busy ? "" : "none";   // the monitor says when the sound is being rebuilt
}
$("cta").onclick = () => { const st = ctaState(); if (st.run) st.run(); };

