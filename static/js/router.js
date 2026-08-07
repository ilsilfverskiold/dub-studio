"use strict";
/* ---------------- plumbing ---------------- */
/* real navigation: every page is a real URL — /projects is the overview, /p/<project-name>
   is the studio for that project. Back/forward walk the history (including across project
   switches), and a pasted /p/... URL opens that project directly. */
function slugOf(p){
  const s = String(p.name || "").toLowerCase()
    .replace(/[^a-z0-9\s-]/g, "").trim().replace(/[\s_]+/g, "-").replace(/-+/g, "-");
  return s || p.id;
}
function pathFor(view){
  return view === "projects" ? "/projects"
       : (S && S.project ? "/p/" + encodeURIComponent(slugOf(S.project)) : "/");
}
function projectFromPath(path){
  if (!path.startsWith("/p/")) return null;
  const slug = decodeURIComponent(path.slice(3).replace(/\/+$/, ""));
  return ((S && S.projects) || []).find(x => slugOf(x) === slug) || null;
}
function nav(view, push=true){
  UI.view = view;
  renderAll();
  if (push) history.pushState({view}, "", pathFor(view));
}
window.addEventListener("popstate", e => {
  UI.view = (e.state && e.state.view) || (location.pathname === "/projects" ? "projects" : "studio");
  // the URL names a project — honor it, without pushing a new entry on top of this one
  const p = projectFromPath(location.pathname);
  if (p && !p.active){ openProject(p.id, false); return; }
  renderAll();
});
if (location.pathname === "/projects") UI.view = "projects";
UI.wantPath = location.pathname.startsWith("/p/") ? location.pathname : null;
history.replaceState({view: UI.view}, "", location.pathname);

