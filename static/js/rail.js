"use strict";
/* ---------------- right rail ---------------- */
function renderRail(){
  $("rt_act").classList.toggle("on", UI.rail==="activity");
  $("rt_takes").classList.toggle("on", UI.rail==="takes");
  const host = $("railbody");
  if (UI.rail === "activity"){
    const colors = {ok:"var(--ok)", done:"var(--ok)", cache:"var(--ok)", warn:"var(--amber)",
                    paid:"var(--amber)", info:"var(--acc)", error:"var(--err)", metric:"var(--ind2)"};
    const sig = "log" + LOG.length;
    if (sigs.rail === sig) return; sigs.rail = sig;
    host.innerHTML = LOG.length ? [...LOG].reverse().map(e => `
      <div class="logrow"><span class="tm">${hms(e.t)}</span>
        <span class="dot" style="background:${colors[e.level]||"var(--acc)"}"></span>
        <div class="body"><span class="msg">${esc(e.message)}</span>
          <span class="det">${esc(e.detail || (e.stage + (e.clip ? " · " + e.clip : "")))}</span></div>
      </div>`).join("")
      : `<div class="empty">Everything the tool does shows up here as it happens — every paid call, every cache hit, every measurement.</div>`;
    return;
  }
  // takes
  const c = cur();
  if (!c){ sigs.rail = null; host.innerHTML = `<div class="empty">Select a clip to see its takes.</div>`; return; }
  const key = c.name + ":" + c.takes + ":" + (c.starred_take||"") + ":" + (c.final||"");
  if (sigs.rail === key) return;
  sigs.rail = key;
  fetch("/api/takes?clip=" + encodeURIComponent(c.name)).then(r=>r.json()).then(j => {
    if (j.error){ host.innerHTML = `<div class="empty">${esc(j.error)}</div>`; return; }
    let h = "";
    if (c.final){
      h += takeCard({name:"Current take", meta:fmtT(c.duration) + (c.stale?" · made before your changes":""),
                     tag:c.stale?"stale":"current", cls:"cur", url:c.final + "?v=" + S.out_mtime,
                     seed:7, dl:c.final});
    }
    (j.takes||[]).forEach((t,i) => {
      const vnames = Object.values(t.voices||{}).map(voiceName).join(", ");
      h += takeCard({name:"Take · " + new Date(t.created*1000).toTimeString().slice(0,5),
                     meta:(vnames ? "voice: " + vnames + " · " : "") + t.reason,
                     tag:t.starred?"pinned":"archived", cls:t.starred?"cur":"",
                     url:t.url, seed:i+11, dl:t.url,
                     star:{id:t.id, on:t.starred}, del:t.starred?null:t.id});
    });
    (j.stems||[]).forEach((t,i) => {
      h += takeCard({name:t.name, meta:"what this layer contributes", tag:"stem", cls:"",
                     url:t.url + "?v=" + S.out_mtime, seed:i+31, dl:t.url});
    });
    host.innerHTML = h || `<div class="empty">No takes yet — Convert renders the first one. Every re-convert archives the old take here.</div>`;
  });
}
function takeCard(o){
  return `<div class="takecard ${o.cls}">
    <div class="head"><b>${esc(o.name)}</b><span class="meta">${esc(o.meta||"")}</span>
      <span class="tag">${o.tag}</span></div>
    <div class="row">
      <button class="playbtn" aria-label="play ${esc(o.name)}"
              onclick="soloPlayUrl('${o.url}')">${PLAY}</button>
      <div class="wave" style="flex:1">${waveHTML(44, o.seed, 20)}</div>
      ${o.star ? `<button class="star${o.star.on?" on":""}" aria-label="${o.star.on?"unpin":"pin"} this take"
        title="${o.star.on?"unpin — go back to the live mix":"pin as the clip's final"}"
        onclick="post('/api/take', {clip:'${UI.clip}', action:'${o.star.on?"unstar":"star"}', id:'${o.star.id}'})">★</button>` : ""}
      ${o.del ? `<button class="dl" aria-label="delete this take" title="delete"
        onclick="post('/api/take', {clip:'${UI.clip}', action:'delete', id:'${o.del}'})">✕</button>` : ""}
      <a class="dl" href="${(o.dl||"").split("?")[0]}" download aria-label="download ${esc(o.name)}" title="download">↓</a>
    </div></div>`;
}
function soloPlayUrl(url){
  if (url.includes("/media/solo/")){ $("preview").src = url; $("preview").play(); return; }
  const v = $("vid");
  if (v){ v.src = url; v.play(); } else { $("preview").src = url; $("preview").play(); }
}

