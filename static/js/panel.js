"use strict";
/* ---------------- stage panel ---------------- */
function voiceName(vid){
  const v = (voicesList||[]).find(x=>x.voice_id===vid);
  return v ? v.name : (vid ? vid.slice(0,10)+"…" : "—");
}
function voiceLabels(vid){
  const v = (voicesList||[]).find(x=>x.voice_id===vid);
  return v ? Object.values(v.labels||{}).slice(0,3).join(", ") : "";
}
function renderPanel(){
  const host = $("panelcol");
  const st = UI.stage ?? autoStage();
  const c = cur();
  const sig = JSON.stringify(["panel", st, c && [c.name, c.stage, c.regions, c.assigns, c.chars, c.voices,
    c.sensitivity, c.mix_override, c.mix_stale, c.est, c.tts_takes, c.voice_dips, c.duck_regions,
    c.boost_regions], UI.region, UI.mixScope, S.master, S.busy, UI.selSeg,
    voicesList ? voicesList.length : 0, mix.master, mix.clip[UI.mixScope], S.cast]);
  if (sigs.panel === sig) return; sigs.panel = sig;
  let h = "";
  if (!c && !(st===3)){
    host.innerHTML = `<div class="empty">Select a clip in the strip above.</div>`; return;
  }

  if (c && st < 3 && UI.region != null && (c.regions||[])[UI.region]){
    const r = c.regions[UI.region], a = (c.assigns||[])[UI.region] || {};
    h += `<div class="rcard">
      <div class="head"><b>REGION ${UI.region+1}${a.character ? " · @"+esc(a.character) : ""}</b>
        <span>${(r.end-r.start).toFixed(2)}s</span></div>
      <div class="rrow">
        <label class="flabel"><span>start</span>
          <input type="number" step="0.05" min="0" value="${r.start}" id="ri_s" class="mono"></label>
        <label class="flabel"><span>end</span>
          <input type="number" step="0.05" min="0" value="${r.end}" id="ri_e" class="mono"></label>
        <button class="playbtn" style="margin:0 4px 1px" aria-label="play this region"
                title="play this region" onclick="previewRegion()">${PLAY}</button>
        ${c.assigns && c.assigns.length ? `
        <label class="flabel" style="flex:1;min-width:120px"><span>speaker</span>
          <select id="ri_ch"><option value="" ${!a.character?"selected":""}>— no one</option>
            ${allChars(c).map(x=>`<option value="${x.identifier}" ${a.character===x.identifier?"selected":""}>@${x.identifier}</option>`).join("")}
          </select></label>
        <label class="flabel" style="width:130px"><span>sound</span>
          <select id="ri_kd">${Object.entries(KINDNAMES).map(([k,v])=>`<option value="${k}" ${a.kind===k?"selected":""}>${v}</option>`).join("")}
          </select></label>
        <label class="flabel" style="width:84px"><span>convert</span>
          <select id="ri_cv" title="yes = the new voice re-performs this region (paid once, then cached). no = the original sound plays, free. Flipping is always free.">
            <option value="1" ${(a.convert !== undefined ? a.convert : convDefault(a, r)) ? "selected" : ""}>yes</option>
            <option value="0" ${(a.convert !== undefined ? a.convert : convDefault(a, r)) ? "" : "selected"}>no</option>
          </select></label>` : ""}
        <div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap">
          <button class="b go" onclick="saveRegion()">Save</button>
          <button class="b" style="color:#9a9aa4" onmouseover="this.style.color='var(--err)'"
                  onmouseout="this.style.color='#9a9aa4'" onclick="deleteRegion()">Delete</button>
        </div>
      </div>
      ${!(c.assigns && c.assigns.length) ? `
      <p class="note" style="margin-top:10px">Speaker and sound tags (words / singing / laughter / SFX…) appear here once this clip has a cast.</p>` : ""}
      </div>`;
  }


  // BAR EDITOR — click any bar (duck, boost, original-voice turn-down) and its amount editor
  // appears here: how much, save, remove. The user decides per bar.
  if (c && UI.selSeg && UI.selSeg.type === "vpiece"){
    const s = UI.selSeg;
    h += `<div class="rcard">
      <div class="head"><b>ADDED VOICE</b><span>${s.ps.toFixed(2)}–${s.pe.toFixed(2)}s</span></div>
      <div class="rrow">
        <button class="b" ${c.solo_voice ? "" : "disabled"}
                onclick="playSoloSpan('${c.solo_voice||""}', ${s.ps}, ${s.pe})">▶ voice only</button>
        <button class="b" onclick="playRegion(${s.ps}, ${s.pe})">▶ in the mix</button>
        <span class="note" style="flex:1;min-width:140px">Hear this span alone to tell a voice problem from a background problem.</span>
        <button class="b" style="color:#9a9aa4" onmouseover="this.style.color='var(--err)'"
                onmouseout="this.style.color='#9a9aa4'" onclick="removeSelected()">Remove voice here</button>
      </div></div>`;
  }
  if (c && UI.selSeg && ["duck","boost","dip"].includes(UI.selSeg.type)){
    const s = UI.selSeg;
    const src = s.type==="duck" ? duckList(c) : s.type==="boost" ? (c.boost_regions||[]) : (c.voice_dips||[]);
    const bar = src[s.i];
    if (bar){
      const names = {duck:"DUCK BAR", boost:"BOOST BAR", dip:"ORIGINAL-VOICE TURN-DOWN"};
      const defs = {duck:Math.abs((S.settings||{}).duck_under_db||28), boost:6, dip:60};
      const verbs = {duck:"turns everything under the new voice DOWN by this much, only inside this bar — 60 removes it entirely (silence)",
                     boost:"turns everything except the new voice UP by this much, only inside this bar",
                     dip:"turns the ORIGINAL voice down by this much, only inside this bar — 60 removes it entirely"};
      h += `<div class="rcard">
        <div class="head"><b>${names[s.type]}</b><span>${bar.start.toFixed(2)}–${bar.end.toFixed(2)}s</span></div>
        <div class="rrow">
          <label class="flabel" style="width:110px"><span>amount (dB)</span>
            <input type="number" step="1" min="1" max="60" value="${Math.round(Math.abs(bar.db ?? defs[s.type]))}" id="bar_db" class="mono"></label>
          <span class="note" style="flex:1;min-width:160px">${verbs[s.type]}.</span>
          <div style="display:flex;gap:7px;align-items:center">
            <button class="b go" onclick="saveBarAmount()">Save</button>
            <button class="b" style="color:#9a9aa4" onmouseover="this.style.color='var(--err)'"
                    onmouseout="this.style.color='#9a9aa4'" onclick="removeSelected()">Remove</button>
          </div>
        </div></div>`;
    }
  }

  h += `<div class="stagecard"><div class="shead"><b>${STAGES[st].title}</b><span>${STAGES[st].hint}</span></div>`;

  if (st === 0 && c){
    const sens = c.sensitivity ?? 100;
    h += `<div style="display:flex;flex-direction:column;gap:16px">
      <div class="rrow">
        <label class="flabel" style="width:130px"><span>speakers in this clip</span>
          <select id="vc_sel" onchange="saveVoices('${c.name}')">${[1,2,3].map(n=>`<option value="${n}" ${c.voices==n?"selected":""}>${n} speaker${n>1?"s":""}</option>`).join("")}</select></label>
        <label class="flabel" style="flex:1;min-width:180px">
          <span>split granularity · <span id="senslbl">${sens}</span>%</span>
          <input type="range" min="10" max="100" value="${sens}" id="sens"
                 oninput="$('senslbl').textContent=this.value"></label>
        <button class="b" onclick="redetect('${c.name}')">Re-detect · free</button>
      </div>
      <p class="note">${(c.regions||[]).length} region(s) on the timeline below${c.voices<=1 ? " — one speaker, so the whole vocal layer converts in a single pass" : ""}.
      Click any block to correct its window. Lower granularity merges same-speaker turns across gaps and drops slivers —
      it re-derives from the cached detection, never re-runs the model, and never re-bills.</p></div>`;
  }

  if (st === 1 && c){
    if (c.chars && c.chars.length){
      const speakers = [...new Set((c.assigns||[]).filter(a=>a.kind==="speech"&&a.character).map(a=>a.character))];
      h += `<p class="note" style="margin-bottom:12px">${speakers.length<=1
        ? "one speaker — the whole vocal layer converts in a single pass"
        : speakers.length + " speakers — each region converts separately with its own voice"}</p>`;
      h += c.chars.map((ch, i) => `
        <div class="charcard">
          <div class="disc"></div>
          <div class="who">
            <span class="id">@${ch.identifier}
              <button class="icb" style="min-width:20px;min-height:20px" aria-label="rename ${ch.identifier}"
                      title="rename" onclick="renameChar('${ch.identifier}')">✎</button></span>
            <span class="sub" title="edit who this character is" onclick="editChar('${ch.identifier}')">${voiceName(ch.voice_id)}${voiceLabels(ch.voice_id) ? " · " + voiceLabels(ch.voice_id) : ""} · ${esc(ch.gender)}, ${esc(ch.age)} ✎</span>
          </div>
          <div class="wave">${waveHTML(40, i+21, 18)}</div>
          <button class="playbtn" aria-label="audition the voice for ${ch.identifier}" onclick="previewVoice('${ch.voice_id}')">${PLAY}</button>
          <button class="b" onclick="openVoiceBrowser('${ch.identifier}')">Change</button>
        </div>
        <div id="chedit_${ch.identifier}"></div>`).join("");
      h += `<div style="display:flex;gap:8px">
        <button class="b dashed" style="flex:1;height:34px" onclick="addChar('${c.name}')">+ add character</button>
        <button class="b" style="height:34px" onclick="recastOne('${c.name}')">Re-cast with Gemini ≈$0.02</button>
      </div>`;
      if (c.assigns && c.assigns.length){
        h += `<div class="eyebrow" style="padding:14px 0 7px">WHO SPEAKS WHERE</div>
          <p class="note" style="margin-bottom:8px">The pipeline labeled each region — correct anything it got wrong.
          The CONVERT dropdown is the decision: "convert" = the new voice re-performs the region
          (paid once at Convert, then cached) · "keep original" = the original sound plays, free,
          shown as a strip on the timeline. The label only sets the starting choice — flipping is
          always free, even after converting.</p>`;
        h += (c.regions||[]).map((r, i) => {
          const a = c.assigns[i] || {};
          const cv = a.convert !== undefined ? !!a.convert : convDefault(a, r);
          return `<div class="layerrow" style="cursor:pointer" onclick="if(!['SELECT','INPUT','LABEL'].includes(event.target.tagName)){UI.region=${i};sigs={};renderAll()}">
            <span class="mono" style="font-size:10.5px;color:var(--faint);width:14px">${i+1}</span>
            <span class="mono" style="font-size:11px;color:#8a8a95;width:92px">${r.start.toFixed(2)}–${r.end.toFixed(2)}s</span>
            <select style="flex:1;min-width:110px" onchange="saveAssignRows('${c.name}')" id="ar_ch_${i}" aria-label="who speaks in region ${i+1}">
              <option value="" ${!a.character?"selected":""}>— no one</option>
              ${allChars(c).map(x=>`<option value="${x.identifier}" ${a.character===x.identifier?"selected":""}>@${x.identifier}</option>`).join("")}
            </select>
            <select style="flex:1;min-width:120px" onchange="saveAssignRows('${c.name}')" id="ar_kd_${i}" aria-label="what kind of sound in region ${i+1}">
              ${Object.entries(KINDNAMES).map(([k,v])=>`<option value="${k}" ${a.kind===k?"selected":""}>${v}</option>`).join("")}
            </select>
            <select style="width:118px;flex:none" onchange="saveAssignRows('${c.name}')" id="ar_cv_${i}"
                    aria-label="convert region ${i+1}"
                    title="yes = the new voice re-performs this region (paid once, then cached). no = the original sound plays, free. Flipping is always free.">
              <option value="1" ${cv?"selected":""}>convert: yes</option>
              <option value="0" ${cv?"":"selected"}>convert: no</option>
            </select>
          </div>`;
        }).join("");
      }
    } else {
      h += `<div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="b" onclick="castOne('${c.name}')">Cast with Gemini ≈$0.02</button>
        <button class="b dashed" onclick="addChar('${c.name}')">+ build the cast myself — free</button></div>
      <p class="note" style="margin-top:10px">Gemini watches the clip and proposes characters — or add your own,
      pick its voice, and assign the regions by hand. Both work the same downstream.</p>`;
    }
    // TTS VOICES WIND UP IN THE CAST (user law): anyone speaking a generated line on this
    // clip's timeline appears here — audition or swap their voice in place. Display + voice
    // only: no region assignments are created, so this can never stale a paid conversion.
    const ttsChars = [...new Set((c.tts_takes||[]).map(t=>t.character).filter(Boolean))]
      .filter(id => !(c.chars||[]).some(ch=>ch.identifier===id));
    if (ttsChars.length){
      h += `<div class="eyebrow" style="padding:14px 0 7px">TTS VOICES IN THIS CLIP</div>`;
      h += ttsChars.map((id, i) => {
        const ch = (S.cast||[]).find(x=>x.identifier===id) || {identifier:id, voice_id:""};
        const wins = (c.tts_takes||[]).filter(t=>t.character===id);
        const stale = wins.some(t=>t.has && t.voice_stale);
        return `<div class="charcard">
          <div class="disc"></div>
          <div class="who">
            <span class="id">@${esc(id)}</span>
            <span class="sub">${voiceName(ch.voice_id)}${voiceLabels(ch.voice_id) ? " · " + voiceLabels(ch.voice_id) : ""} · speaks ${wins.length} TTS window${wins.length===1?"":"s"}${
              stale ? ` · <span style="color:var(--amber)">a take still uses a previous voice — open it and Regenerate</span>` : ""}</span>
          </div>
          <div class="wave">${waveHTML(40, i+57, 18)}</div>
          <button class="playbtn" aria-label="audition the voice for ${esc(id)}" onclick="previewVoice('${ch.voice_id}')">${PLAY}</button>
          <button class="b" onclick="openVoiceBrowser('${esc(id)}')">Change</button>
        </div>`;
      }).join("");
    }
  }

  if (st === 2 && c){
    // mirrors the pipeline's convert gate: the region's own Convert field first, else the
    // kind defaults (words always; laughter/singing only at >= 1.2 s)
    const conv = (c.assigns||[]).filter((a, i) => {
      const r = (c.regions||[])[i] || {start:0, end:0};
      return a.convert !== undefined ? !!a.convert : convDefault(a, r);
    }).length || (c.regions||[]).length;
    const done = c.stage === "converted" && !c.stale;
    if (done){
      h += `<div style="display:flex;flex-direction:column;gap:14px">
        <div class="banner"><span class="msg">This clip is converted — the dubbed take is what's playing
          in the monitor. Its sound is in the Mix stage; earlier versions are in the Layers rail.</span>
          <span class="meta">converted · current</span></div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <button class="b" onclick="convertOne('${c.name}')">Re-convert this clip</button>
          <span class="note">the current take is archived first; unchanged steps come from cache ($0)</span>
        </div></div>`;
    } else {
      h += `<div style="display:flex;flex-direction:column;gap:14px">
        <div class="tiles">
          <div class="tile"><span>regions to render</span><b>${conv}</b></div>
          <div class="tile"><span>estimated credits</span><b>≈ ${c.est.credits||0}</b></div>
          <div class="tile"><span>estimated cost</span><b style="color:var(--amber)">≈ $${(c.est.el_usd||0).toFixed(2)}</b></div>
        </div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <button class="b go" style="height:36px;padding:0 18px" onclick="convertOne('${c.name}')">${c.final ? "Re-convert this clip" : "Convert this clip"}</button>
          <span class="note">runs on your own keys — cached steps are reused, only changed regions re-render</span>
        </div>
        ${c.final && c.stale ? `<p class="note">The current dubbed take was made before your latest changes — converting updates it (the old one is archived in Layers).</p>` : ""}
      </div>`;
    }
  }

  if (st === 3 && !S.clips.some(x => x.stage === "converted" || x.final)){
    // nothing converted -> there is nothing to mix or render; say so instead of showing dead faders
    h += `<div class="empty" style="border:none;padding:14px">Nothing to mix yet.<br>
      ${S.clips.length ? "Convert a clip first — then its layers and the master appear here."
                       : "Add clips, detect, cast and convert — then the mixing desk lives here."}</div></div>`;
    host.innerHTML = h;
    return;
  }
  if (st === 3){
    const scopeMaster = UI.mixScope === "master";
    const target = scopeMaster ? null : S.clips.find(x=>x.name===UI.mixScope);
    const vals = scopeMaster
      ? (mix.master || S.settings)
      : (mix.clip[UI.mixScope] || (target ? target.mix_settings : S.settings));
    const banner = scopeMaster
      ? `The finished cut, loudness-normalized for delivery — so faders here set the BALANCE between voice and background. To hear absolute level changes, audition a single clip.`
      : `Mixing ${UI.mixScope} on its own — its settings ride on top of the master for this clip only.`;
    const dirty = (scopeMaster ? !!mix.master : !!mix.clip[UI.mixScope])
      || (scopeMaster ? S.master.state === "stale"
                      : !!(target && (target.mix_stale || target.stale)));
    if (dirty){
      // the whole banner changes state — one amber sentence, not a note squeezed into a corner
      h += `<div class="banner" style="border-color:rgba(240,185,85,.3);background:rgba(240,185,85,.07)">
        <span class="msg" style="color:var(--amber)">Unrendered changes — you're hearing the last render.</span></div>`;
    } else {
      const meta = scopeMaster
        ? `${S.clips.length} clips · voice ${vals.dialog_db} dB · background ${vals.bed_db} dB`
        : `${target && target.mix_override ? "has its own mix" : "matches master"} · voice ${vals.dialog_db} dB`;
      h += `<div class="banner"><span class="msg">${banner}</span><span class="meta">${meta}</span></div>`;
    }
    if (!scopeMaster){
      h += `<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 12px;border-radius:9px;border:1px dashed rgba(255,255,255,.1);margin-bottom:14px">
        <span class="note">${target && target.mix_override ? "This clip differs from the master mix." : "This clip currently matches the master mix — move a fader to give it its own."}</span>
        <button class="b sm" style="margin-left:auto" onclick="matchMaster('${UI.mixScope}')" ${target && target.mix_override ? "" : "disabled"}>Match master</button>
      </div>`;
    } else {
      h += `<div class="eyebrow" style="padding:0 0 7px">LAYERS</div>` +
        [["New voice (STS/TTS)","var(--acc)","dialog_db"," dB"],
         ["Background (music/SFX)","var(--ok)","bed_db"," dB"],
         ["Duck depth (inside bars)","var(--amber)","duck_under_db"," dB dip"]].map(([nm,sw,k,unit]) => {
          const f = FADERS.find(x=>x[0]===k);
          return `<div class="layerrow"><span class="sw" style="background:${sw}"></span>
            <span class="nm">${nm}</span>
            <input type="range" min="${f[1]}" max="${f[2]}" step="${f[3]}" value="${vals[k]}"
                   oninput="fader('${k}', this.value)">
            <span class="db" id="lv_${k}">${vals[k] > 0 ? "+" : ""}${vals[k]}${unit}</span>
            ${k!=="duck_under_db" && c && c.solo_voice ? `<button class="lchip" onclick="soloPlay('${k==="dialog_db" ? c.solo_voice : c.solo_bed}')">listen</button>` : ""}
          </div>`;
        }).join("") +
        `<p class="note" style="margin:2px 0 0;font-size:10.5px">The fader number is the MEASURED level —
         every render logs "voice X dB · background Y dB" so you can check it. Duck depth acts only inside
         the bars you can see on the timeline.</p>`;
    }
    const D = S.defaults || {};
    const faderDefs = scopeMaster
      ? [["master","MASTER"],
         ["reel_lufs",-24,-12,.5,"Master loudness (delivery)","How loud the finished master is overall. The whole program is moved by ONE flat measured gain to hit this — every clip moves by the same amount, so nothing you balanced can shift between clips."],
         ...FADERS]
      : FADERS;
    h += `<div class="faders" style="margin-top:14px">` + faderDefs.map(f => {
      if (f.length === 2) return `<div class="fgroup">${f[1]}</div>`;
      const [k,min,max,step,label,note] = f;
      const atDefault = D[k] === undefined || +vals[k] === +D[k];
      return `<div class="fader">
        <div class="top"><span>${label}</span>
          <span style="display:inline-flex;align-items:center;gap:5px">
            <button class="icb" id="rst_${k}" onclick="resetKnob('${k}')" aria-label="reset ${label} to our default"
              title="back to our default (${D[k]})" style="min-width:20px;min-height:20px;font-size:11px;
              visibility:${atDefault ? "hidden" : "visible"}">↺</button>
            <b id="fv_${k}">${vals[k]}</b></span></div>
        <input type="range" id="fk_${k}" min="${min}" max="${max}" step="${step}" value="${vals[k]}"
               oninput="fader('${k}', this.value)">
        <p>${note}</p></div>`;
    }).join("") + `
      <div class="fader" style="display:flex;align-items:center;gap:10px">
        <input type="checkbox" id="k_duck" ${vals.duck?"checked":""} onchange="fader('duck', this.checked)">
        <div><div class="top" style="margin:0"><span>Propose duck bars under voices</span></div>
        <p style="margin-top:2px">On = when a clip has no duck map yet, the tool DRAWS duck bars where a voice speaks — on the timeline, yours to move or delete. Off = it draws nothing. Ducking itself only ever happens inside visible bars.</p></div>
      </div></div>`;
    // Render is only offered when it would DO something: settings moved, or the master is stale.
    // A current master shows a quiet "Master is current" instead of an always-pressable button.
    const canRender = dirty || S.master.state !== "ok";
    h += `<div class="mixfoot">` + (scopeMaster
      ? `<button class="b go" id="rendbtn" style="height:36px;padding:0 18px" onclick="renderMaster()"
           title="bounces the full program to one file — free, clips are not re-converted"
           ${canRender && !S.busy ? "" : "disabled"}>${canRender ? "Render master" : "Master is current"}</button>
         <button class="b" style="height:36px" onclick="downloadMaster()" ${S.master.state==="ok" ? "" : "disabled"}>Download master</button>
         <button class="b" style="height:36px;margin-left:auto" onclick="resetAllMix()" ${S.busy ? "disabled" : ""}
           title="every knob back to our defaults — master and every clip (clips return to Match master). Free — conversions and takes are untouched; anything stale re-renders.">Reset all knobs</button>`
      : (() => {
          const faderDirty = !!mix.clip[UI.mixScope];
          const clipStale = !!(target && target.mix_stale);
          // faders moved -> save them; duck/mute changed -> render applies them; else nothing to do
          if (faderDirty)
            return `<button class="b go" id="savemixbtn" style="height:36px;padding:0 18px"
              onclick="saveClipMix('${UI.mixScope}')" ${S.busy ? "disabled" : ""}>Save clip mix · free</button>
              <span class="note">re-mixes only this clip; the master goes stale until you re-render it</span>`;
          if (clipStale)
            return `<button class="b go" id="savemixbtn" style="height:36px;padding:0 18px"
              onclick="post('/api/render-master')" ${S.busy ? "disabled" : ""}>Render clip · free</button>
              <span class="note">applies your duck / voice changes to this clip's mix</span>`;
          return `<button class="b go" id="savemixbtn" style="height:36px;padding:0 18px" disabled>Save clip mix · free</button>
              <span class="note">move a fader or edit the timeline lines — then apply here</span>`;
        })())
      + `</div>`;
  }
  h += `</div>`;
  host.innerHTML = h;
}
function fader(k, v){
  const scopeMaster = UI.mixScope === "master" || (UI.stage ?? autoStage()) !== 3;
  const target = scopeMaster ? null : S.clips.find(x=>x.name===UI.mixScope);
  if (scopeMaster){
    if (!mix.master) mix.master = {...S.settings};
    mix.master[k] = k === "duck" ? !!v : parseFloat(v);
  } else {
    if (!mix.clip[UI.mixScope]) mix.clip[UI.mixScope] = {...(target ? target.mix_settings : S.settings)};
    mix.clip[UI.mixScope][k] = k === "duck" ? !!v : parseFloat(v);
  }
  const el = $("fv_"+k); if (el) el.textContent = v;
  const lv = $("lv_"+k); if (lv) lv.textContent = (v>0?"+":"")+v+(k==="duck_under_db"?" dB dip":" dB");
  const rb = $("rendbtn");
  if (rb){ rb.disabled = false; rb.textContent = "Render master · free"; }
  const sb = $("savemixbtn");
  if (sb) sb.disabled = false;
  const rs = $("rst_"+k);
  if (rs && S.defaults) rs.style.visibility = (+v === +S.defaults[k]) ? "hidden" : "visible";
  const bn = document.querySelector(".banner");
  if (bn){
    bn.style.borderColor = "rgba(240,185,85,.3)";
    bn.style.background = "rgba(240,185,85,.07)";
    bn.innerHTML = '<span class="msg" style="color:var(--amber)">Unrendered changes — you\'re hearing the last render.</span>';
  }
}
function resetKnob(k){
  const d = (S.defaults || {})[k];
  if (d === undefined) return;
  const inp = $("fk_"+k);
  if (inp) inp.value = d;
  fader(k, d);
  sigs.panel = null; renderPanel();      // realigns the layer sliders that mirror these knobs
}
function renderMaster(){
  const body = mix.master ? {settings: mix.master} : {};
  post("/api/remix", body).then(()=>{ mix.master = null; });
}
function saveClipMix(name){
  const s = mix.clip[name];
  if (!s){ toast("move a fader first — nothing changed"); return; }
  post("/api/clip-mix", {clip:name, settings:s}).then(()=>{ delete mix.clip[name]; });
}
function matchMaster(name){
  delete mix.clip[name];
  post("/api/clip-mix", {clip:name, match_master:true});
}
function resetAllMix(){
  post("/api/mix-reset", {}).then(()=>{ mix.master = null; mix.clip = {};
                                        toast("every knob back to defaults"); });
}
function downloadMaster(){
  const url = S.reel || (S.clips.find(c=>c.final)||{}).final;
  if (url){ const a = document.createElement("a"); a.href = url.split("?")[0]; a.download = ""; a.click(); }
}
function soloPlay(url){ $("preview").src = url + "?v=" + S.out_mtime; $("preview").play(); }
function playSoloSpan(url, a, b){
  // play ONE span of a solo stem — the debug ear: is the problem the voice or the background?
  const p = $("preview");
  p.pause();
  p.src = url + "?v=" + S.out_mtime;
  p.onloadedmetadata = () => { p.currentTime = a; p.play(); };
  const stop = () => { if (p.currentTime >= b){ p.pause(); p.removeEventListener("timeupdate", stop); } };
  p.addEventListener("timeupdate", stop);
  p.load();
}

/* region ops (timeline is the list; inspector card edits the selection) */
function previewRegion(){
  const c = cur(); if (!c || UI.region == null) return;
  playRegion(parseFloat($("ri_s").value), parseFloat($("ri_e").value));
}
function saveRegion(){
  const c = cur(); if (!c || UI.region == null) return;
  pushUndo(c);
  const s = parseFloat($("ri_s").value), e = parseFloat($("ri_e").value);
  if (isNaN(s) || isNaN(e) || e <= s){ toast("end must be after start"); return; }
  const regions = c.regions.map((r,j)=> j===UI.region ? {start:s, end:e} : r);
  const chE = $("ri_ch"), kdE = $("ri_kd"), cvE = $("ri_cv");
  const i = UI.region, name = c.name;
  const assigns = (chE && c.assigns && c.assigns.length)
      ? c.assigns.map((a,j)=> j===i
          ? {character:chE.value, kind:kdE.value, convert: cvE ? cvE.value === "1" : undefined}
          : a) : null;
  post("/api/regions", {clip:name, regions, voices:c.voices})
    .then(()=> assigns && post("/api/assign", {clip:name, assigns}));
}
function deleteRegion(){
  const c = cur(); if (!c || UI.region == null) return;
  pushUndo(c);
  const regions = c.regions.filter((_,j)=> j!==UI.region);
  c.regions = regions;
  if (c.assigns) c.assigns = c.assigns.filter((_,j)=> j!==UI.region);
  UI.region = null;
  sigs = {}; renderAll();
  post("/api/regions", {clip:c.name, regions, voices:c.voices});
}
async function addRegion(){
  const c = cur(); if (!c) return;
  pushUndo(c);
  const last = (c.regions||[])[c.regions.length-1];
  const a = last ? Math.min(last.end + 0.2, (c.duration||1) - 0.8) : 0.2;
  const regions = [...(c.regions||[]), {start:+a.toFixed(2), end:+Math.min(a+1.2, c.duration||1).toFixed(2)}];
  // the user says at creation whether this region converts — and can flip it anytime, free
  let conv = true;
  if ((c.chars||[]).length){
    conv = await ask({title:"Convert this region?",
      detail:"Run = the new voice re-performs it (paid once at Convert, then cached). " +
             "Cancel = KEEP the original sound there — free, shown as an original-sound strip.",
      note:"Flip it anytime on the region's row — flipping is always free.",
      action:"Convert this region"});
  }
  await post("/api/regions", {clip:c.name, regions, voices:c.voices});
  if ((c.chars||[]).length && !conv){
    const asn = [...(c.assigns||[])];
    while (asn.length < regions.length)
      asn.push({character:((c.chars||[])[0]||{}).identifier||"", kind:"speech"});
    asn[regions.length-1] = {...asn[regions.length-1], convert:false};
    await post("/api/assign", {clip:c.name, assigns: asn});
  }
}
function saveVoices(name){
  const c = S.clips.find(x=>x.name===name);
  pushUndo(c);
  post("/api/regions", {clip:name, regions:c.regions, voices:parseInt($("vc_sel").value)});
}
function redetect(name){
  pushUndo(S.clips.find(x=>x.name===name));
  post("/api/redetect", {clip:name, sensitivity: parseInt($("sens").value)});
}
function playRegion(start, end){
  const v = $("vid");
  if (!v) return;
  v.currentTime = start; v.play();
  const stop = () => { if (v.currentTime >= end){ v.pause(); v.removeEventListener("timeupdate", stop);} };
  v.addEventListener("timeupdate", stop);
}

/* characters */
function allChars(c){
  // any character introduced ANYWHERE in the project — this clip's cast first, then the rest
  const seen = new Set(), out = [];
  [...(c.chars||[]), ...(S.cast||[])].forEach(ch => {
    if (!ch.identifier || seen.has(ch.identifier)) return;
    seen.add(ch.identifier);
    out.push(ch);
  });
  return out;
}
function convDefault(a, r){
  // mirror of the engine's default when the Convert field was never set:
  // words always; laughter/singing only when 1.2 s or longer
  const dur = r ? (r.end - r.start) : 0;
  return a.kind === "speech" ||
         ((a.kind === "singing" || a.kind === "human_nonword") && dur >= 1.2);
}
function saveAssignRows(name){
  const c = S.clips.find(x=>x.name===name);
  pushUndo(c);
  const assigns = (c.regions||[]).map((r, i) => ({
    character: ($("ar_ch_"+i)||{}).value || "",
    kind: ($("ar_kd_"+i)||{}).value || "speech",
    convert: ($("ar_cv_"+i)||{value:"1"}).value === "1",
  }));
  c.assigns = assigns;                          // optimistic — the timeline tags update instantly
  sigs.tl = null; renderTimeline();
  post("/api/assign", {clip:name, assigns});
}
async function addChar(name){
  await post("/api/char", {action:"add", identifier:"char_" + (Date.now()%1000), clip:name});
}
async function renameChar(id){
  const nv = await askText({title:"Rename character", value:id,
                            note:"The @identifier — renamed everywhere it appears, in every clip.",
                            action:"Rename"});
  if (nv === null) return;
  const v = nv.trim().replace(/^@+/,"");
  if (v && v !== id) post("/api/char", {action:"rename", identifier:id, new_identifier:v});
}
function editChar(id){
  let ch = null;
  for (const c of S.clips) for (const x of (c.chars||[])) if (x.identifier === id) ch = x;
  const host = $("chedit_"+id);
  if (!ch || !host) return;
  if (host.innerHTML){ host.innerHTML = ""; return; }
  const sl = (nm, opts, curv) => `<select id="ce_${nm}">` +
    opts.map(o=>`<option value="${o}" ${o===curv?"selected":""}>${o.replace("_"," ")}</option>`).join("") + `</select>`;
  host.innerHTML = `<div class="chedit">
    <input type="text" id="ce_description" value="${esc(ch.description||"")}" placeholder="description (young woman, robot, narrator…)" style="flex:1;min-width:150px">
    ${sl("gender", ["male","female","not-important"], ch.gender)}
    ${sl("age", ["young","middle_aged","old","not-important"], ch.age)}
    <input type="text" id="ce_language" value="${esc(ch.language||"")}" placeholder="en" title="language code" style="width:52px">
    ${sl("role", ["main","supporting"], ch.role)}
    <button class="b go sm" onclick="saveChar('${id}')">Save</button></div>`;
}
function saveChar(id){
  const f = k => document.getElementById("ce_"+k).value.trim();
  post("/api/char", {action:"update", identifier:id,
    fields:{description:f("description"), gender:f("gender"), age:f("age"),
            language:f("language"), role:f("role")}});
}
async function castOne(n){
  if (await ask({title:"Identify the cast", cost:"≈ $0.02", detail:n, action:"Cast this clip"}))
    post("/api/identify", {approved:true, clip:n});
}
async function recastOne(n){
  if (await ask({title:"Re-cast from scratch", cost:"≈ $0.02",
                 detail:`${n} — Gemini redoes the whole cast. Your region edits stay; character/voice assignments are replaced. The current version is archived to Layers first.`,
                 action:"Re-cast this clip"}))
    post("/api/identify", {approved:true, clip:n, force:true});
}
async function convertOne(n){
  const c = S.clips.find(x=>x.name===n);
  if (await ask({title:"Convert with ElevenLabs", cost:`≈ $${(c.est.el_usd||0).toFixed(2)}`,
                 detail:`${n} — cached steps are skipped and shown live`, action:"Convert this clip"}))
    post("/api/convert", {approved:true, clip:n});
}

/* voices */
function ensureVoices(){
  if (voicesList) return Promise.resolve();
  return fetch("/api/voices").then(r=>r.json()).then(j=>{
    voicesList = j.voices || [];
    sigs.panel = null; renderPanel();
  });
}
function previewVoice(vid){
  ensureVoices().then(()=>{
    const v = (voicesList||[]).find(x=>x.voice_id===vid);
    if (v && v.preview){ $("preview").src = v.preview; $("preview").play(); }
    else toast("no preview available for this voice id");
  });
}
function openVoiceBrowser(identifier){
  vTarget = identifier;
  $("vfor").textContent = "@"+identifier;
  $("vpaste").value = "";
  $("vbrowsebg").classList.add("show");
  $("vlist").innerHTML = "loading voices…";
  ensureVoices().then(()=>{
    let curv = "";
    for (const c of S.clips)
      for (const ch of (c.chars||[]))
        if (ch.identifier === identifier) curv = ch.voice_id;
    for (const ch of (S.cast||[]))       // the registry is the voice authority — it wins
      if (ch.identifier === identifier && ch.voice_id) curv = ch.voice_id;
    const rows = [...voicesList].sort((a,b)=>(b.voice_id===curv)-(a.voice_id===curv));
    $("vlist").innerHTML = rows.map(v=>`
      <div class="vrow" style="${v.voice_id===curv?"background:rgba(70,77,205,.09)":""}">
        <button class="playbtn" aria-label="preview ${esc(v.name)}"
                onclick="$('preview').src='${v.preview||""}';$('preview').play()">${PLAY}</button>
        <span style="min-width:140px">${esc(v.name)}</span>
        ${v.voice_id===curv?'<span class="vchip">current</span>':""}
        <span class="lb" style="flex:1">${esc(Object.values(v.labels).join(" · "))}</span>
        <button class="b go sm" onclick="chooseVoice('${v.voice_id}')">${v.voice_id===curv?"Keep":"Use"}</button>
      </div>`).join("") || "no voices on the account";
  });
}
function chooseVoice(vid){
  $("vbrowsebg").classList.remove("show");
  for (const c of (S?.clips||[]))                 // optimistic: show the choice IMMEDIATELY
    for (const ch of (c.chars||[]))
      if (ch.identifier === vTarget) ch.voice_id = vid;
  for (const ch of (S?.cast||[]))                 // the project registry rows tell it too
    if (ch.identifier === vTarget) ch.voice_id = vid;
  sigs.panel = null; renderPanel();
  // an open TTS modal's spoken-by labels must tell the same truth, instantly
  if ($("ttsbg").classList.contains("show") && cur()) ttCharOptions(cur(), $("tt_char").value);
  post("/api/voice", {identifier: vTarget, voice_id: vid});
}
$("vuse").onclick = () => chooseVoice($("vpaste").value.trim());
$("vclose").onclick = () => $("vbrowsebg").classList.remove("show");
$("vbrowsebg").onclick = e => { if (e.target === $("vbrowsebg")) $("vbrowsebg").classList.remove("show"); };

