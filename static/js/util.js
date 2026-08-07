"use strict";
function post(url, body){
  return fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
                     body: body ? JSON.stringify(body) : "{}"})
    .then(async r => { if(!r.ok){ const e = await r.json().catch(()=>({error:"request failed"})); toast(e.error);} })
    .then(refresh);
}
function fmtT(t){ t = Math.max(0, t||0); return Math.floor(t/60) + ":" + String(Math.floor(t%60)).padStart(2,"0"); }
function hms(ts){ const d = new Date(ts*1000); return String(d.getHours()).padStart(2,"0")+":"+
  String(d.getMinutes()).padStart(2,"0")+":"+String(d.getSeconds()).padStart(2,"0"); }
function cur(){ return S && S.clips.find(c => c.name === UI.clip); }
function bars(n, seed){
  const out = []; let x = seed*9301 + 49297;
  for (let i=0;i<n;i++){ x = (x*9301+49297)%233280;
    const r = x/233280, env = Math.sin((i/n)*Math.PI);
    out.push(0.16 + r*0.74*(0.45+env*0.72)); }
  return out;
}
function waveHTML(n, seed, h){ return bars(n, seed).map(v=>`<i style="height:${(3+v*h).toFixed(1)}px"></i>`).join(""); }
function esc(s){ return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;"); }
/* icon geometry measured from the design's rendered thumbnail — do not resize */
const PLAY = '<svg viewBox="0 0 8 9" aria-hidden="true"><path d="M0 0l8 4.5L0 9z" fill="currentColor"/></svg>';
const PAUSE = '<svg viewBox="0 0 8 9" aria-hidden="true" style="margin-left:0"><path d="M0.5 0h2.4v9H0.5zM5.1 0h2.4v9H5.1z" fill="currentColor"/></svg>';

