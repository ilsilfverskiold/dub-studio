"use strict";
/* ---------------- ask dialog ---------------- */
let askResolve = null;
function ask(o){
  const danger = o.tone === "danger";
  $("mtitle").textContent = o.title;
  $("mtitle").style.color = danger ? "var(--err)" : "var(--amber)";
  document.querySelector("#modalbg .modal").style.borderColor =
      danger ? "rgba(255,123,123,.55)" : "rgba(240,185,85,.55)";
  $("est").style.display = o.cost ? "" : "none";
  $("est").textContent = o.cost || "";
  $("estdetail").textContent = o.detail || "";
  $("mnote").textContent = o.note !== undefined ? o.note :
    "Charged by the AI providers, on your own keys. Adjusting the sound afterwards is free.";
  $("approve").textContent = o.action;
  $("approve").className = "b " + (danger ? "danger" : "warn");
  $("approve").style.flex = "1";
  $("modalbg").classList.add("show");
  return new Promise(res => { askResolve = res; });
}
function closeAsk(v){ $("modalbg").classList.remove("show"); if (askResolve){ askResolve(v); askResolve = null; } }
$("cancel").onclick = () => closeAsk(false);
$("modalbg").onclick = e => { if (e.target === $("modalbg")) closeAsk(false); };
$("approve").onclick = () => closeAsk(true);

/* our own text dialog + toasts — the browser's prompt/alert never appear */
let promptResolve = null;
function askText(o){
  $("ptitle").textContent = o.title;
  $("pnote").textContent = o.note || "";
  const inp = $("pinput");
  inp.value = o.value || ""; inp.placeholder = o.placeholder || "";
  $("pok").textContent = o.action || "Save";
  $("promptbg").classList.add("show");
  setTimeout(() => { inp.focus(); inp.select(); }, 30);
  return new Promise(res => { promptResolve = res; });
}
function closePrompt(v){ $("promptbg").classList.remove("show");
  if (promptResolve){ promptResolve(v); promptResolve = null; } }
$("pok").onclick = () => closePrompt($("pinput").value);
$("pcancel").onclick = () => closePrompt(null);
$("promptbg").onclick = e => { if (e.target === $("promptbg")) closePrompt(null); };
$("pinput").onkeydown = e => {
  if (e.key === "Enter") closePrompt($("pinput").value);
  if (e.key === "Escape") closePrompt(null);
};
function toast(msg, tone){
  const t = document.createElement("div");
  t.className = "toast" + (tone === "info" ? " info" : "");
  t.textContent = msg;
  $("toasthost").appendChild(t);
  setTimeout(() => t.classList.add("out"), 3800);
  setTimeout(() => t.remove(), 4200);
}

