"use strict";
const $ = (id) => document.getElementById(id);
const PAGE = 10;
let skip = 0, total = 0, query = "", editingId = null;

// ---------- helpers ----------
function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = "toast"), 3500);
}
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}
const jsonOpts = (method, body) => ({
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  kids.forEach((c) => n.append(c));
  return n;
};
const fmtDur = (s) => (s == null ? "" : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);

// ---------- history (CRUD) ----------
function renderItem(r) {
  const open = el("div", { class: "item" });
  const text = el("div", { class: "text" }, r.text); // textContent only: no HTML injection
  const meta = el("div", { class: "meta" },
    el("span", {}, new Date(r.created_at).toLocaleString()),
    el("span", { class: "badge" }, r.source),
    ...(r.language ? [el("span", { class: "badge" }, r.language)] : []),
    ...(r.duration_seconds != null ? [el("span", {}, fmtDur(r.duration_seconds))] : []),
  );
  const actions = el("div", { class: "actions" },
    el("button", { class: "small", onclick: () => open.classList.toggle("open") }, "Expand"),
    el("button", { class: "small", onclick: () => openEditor(r) }, "Edit"),
    el("button", {
      class: "small",
      onclick: async () => { await navigator.clipboard.writeText(r.text); toast("Copied"); },
    }, "Copy"),
    el("a", { href: `/api/transcriptions/${r.id}/download` }, el("button", { class: "small", type: "button" }, "Download")),
    el("button", { class: "small danger", onclick: () => remove(r) }, "Delete"),
  );
  open.append(el("h3", {}, r.title), meta, text, actions);
  return open;
}

async function load() {
  try {
    const params = new URLSearchParams({ skip, limit: PAGE });
    if (query) params.set("q", query);
    const data = await api(`/api/transcriptions?${params}`);
    total = data.total;
    if (data.items.length === 0 && skip > 0) { skip = Math.max(0, skip - PAGE); return load(); }
    const list = $("list");
    list.replaceChildren();
    if (!data.items.length) {
      list.append(el("div", { class: "empty" }, query ? "No matches." : "No transcriptions yet. Record one above."));
    } else data.items.forEach((r) => list.append(renderItem(r)));
    $("count").textContent = `(${total})`;
    $("prev").disabled = skip === 0;
    $("next").disabled = skip + PAGE >= total;
    $("page-info").textContent = total ? `${skip + 1}–${Math.min(skip + PAGE, total)} of ${total}` : "";
  } catch (e) { toast(e.message, true); }
}

async function remove(r) {
  if (!confirm(`Delete "${r.title}"?`)) return;
  try { await api(`/api/transcriptions/${r.id}`, { method: "DELETE" }); toast("Deleted"); load(); }
  catch (e) { toast(e.message, true); }
}

function openEditor(r) {
  editingId = r ? r.id : null;
  $("editor-h").textContent = r ? "Edit transcription" : "New entry";
  $("e-title").value = r ? r.title : "";
  $("e-lang").value = r ? r.language || "" : "";
  $("e-text").value = r ? r.text : "";
  $("editor").showModal();
}
$("e-cancel").onclick = () => $("editor").close();
$("new-btn").onclick = () => openEditor(null);
$("editor-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = { title: $("e-title").value, text: $("e-text").value, language: $("e-lang").value || null };
  try {
    if (editingId) await api(`/api/transcriptions/${editingId}`, jsonOpts("PUT", body));
    else await api("/api/transcriptions", jsonOpts("POST", body));
    $("editor").close();
    toast("Saved");
    load();
  } catch (e) { toast(e.message, true); }
});

let st;
$("search").addEventListener("input", (e) => {
  clearTimeout(st);
  st = setTimeout(() => { query = e.target.value.trim(); skip = 0; load(); }, 250);
});
$("prev").onclick = () => { skip = Math.max(0, skip - PAGE); load(); };
$("next").onclick = () => { skip += PAGE; load(); };

// ---------- file upload ----------
$("upload-btn").onclick = async () => {
  const f = $("file").files[0];
  if (!f) return toast("Choose a file first", true);
  const fd = new FormData();
  fd.append("file", f);
  fd.append("mode", $("mode").value);
  $("upload-btn").disabled = true;
  setStatus("Transcribing file… this can take a while on CPU.");
  try {
    await api("/api/transcribe-file", { method: "POST", body: fd });
    toast("File transcribed");
    skip = 0; load();
    setStatus("Done.");
  } catch (e) { toast(e.message, true); setStatus("Upload failed."); }
  finally { $("upload-btn").disabled = false; }
};

// ---------- live transcription ----------
const TARGET_RATE = 16000;
let ws, ctx, stream, node, recording = false, finalText = "";

function setStatus(s) { $("status").textContent = s; }
function renderLive(partial = "") {
  $("live-final").textContent = finalText ? finalText + " " : "";
  $("live-partial").textContent = partial;
  $("live").classList.toggle("has-text", !!(finalText || partial));
}

// Convert float32 at any sample rate to int16 at 16 kHz (box-filter downsample).
function toPCM16(float32, inRate) {
  let data = float32;
  if (inRate !== TARGET_RATE) {
    const ratio = inRate / TARGET_RATE;
    const out = new Float32Array(Math.floor(float32.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const a = Math.floor(i * ratio), b = Math.min(float32.length, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = a; j < b; j++) sum += float32[j];
      out[i] = sum / Math.max(1, b - a);
    }
    data = out;
  }
  const pcm = new Int16Array(data.length);
  for (let i = 0; i < data.length; i++) {
    const s = Math.max(-1, Math.min(1, data[i]));
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return pcm.buffer;
}

async function startRecording() {
  if (!navigator.mediaDevices?.getUserMedia)
    return toast("Microphone needs HTTPS (or localhost).", true);

  const recButton = $("rec");
  recButton.disabled = true;
  setStatus("Requesting microphone permission…");
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (err) {
    recButton.disabled = false;
    setStatus("Microphone unavailable.");
    toast(`Microphone access failed: ${err.message || err.name}`, true);
    return;
  }

  finalText = "";
  renderLive();
  setStatus("Connecting to transcription server…");
  const proto = location.protocol === "https:" ? "wss" : "ws";
  try {
    ws = new WebSocket(`${proto}://${location.host}/ws/transcribe`);
  } catch (err) {
    finishUI();
    setStatus("Could not create WebSocket connection.");
    toast(err.message || "WebSocket connection failed", true);
    return;
  }
  ws.binaryType = "arraybuffer";

  let readyReceived = false;
  const readyTimer = setTimeout(() => {
    if (!readyReceived) {
      setStatus("Server did not complete the transcription handshake.");
      toast("Server connected but did not send its ready response. Check server logs.", true);
      finishUI();
      if (ws && ws.readyState < WebSocket.CLOSING) ws.close();
    }
  }, 10000);

  ws.onopen = () => {
    setStatus("Connected. Initializing transcription…");
    ws.send(JSON.stringify({ type: "start", mode: $("mode").value, title: $("title").value }));
  };
  ws.onmessage = async (m) => {
    let ev;
    try {
      ev = JSON.parse(m.data);
    } catch (err) {
      toast("Received an invalid response from the transcription server.", true);
      setStatus("Invalid server response.");
      finishUI();
      return;
    }
    if (ev.type === "ready") {
      readyReceived = true;
      clearTimeout(readyTimer);
      try {
        await beginCapture();
      } catch (err) {
        console.error("Audio capture initialization failed:", err);
        setStatus(`Audio setup failed: ${err.message || err.name}`);
        toast(`Audio setup failed: ${err.message || err.name}`, true);
        finishUI();
        if (ws && ws.readyState === WebSocket.OPEN) ws.close();
      }
    } else if (ev.type === "partial") renderLive(ev.text);
    else if (ev.type === "final") {
      finalText += (finalText ? " " : "") + ev.text;
      renderLive();
    } else if (ev.type === "saved") {
      finishUI();
      if (ev.transcription) { toast("Saved to history"); skip = 0; load(); setStatus("Saved."); }
      else setStatus("No speech detected, nothing saved.");
      ws.close();
    } else if (ev.type === "error") {
      toast(ev.message || "Transcription server error", true);
      setStatus(`Server error: ${ev.message || "unknown error"}`);
      finishUI();
    }
  };
  ws.onerror = () => {
    clearTimeout(readyTimer);
    console.error("WebSocket transport error");
    toast("WebSocket connection failed. Check the server logs.", true);
    setStatus("WebSocket connection failed.");
    finishUI();
  };
  ws.onclose = (event) => {
    clearTimeout(readyTimer);
    if (recording) {
      finishUI();
      setStatus(`Connection closed (code ${event.code}).`);
    }
    if (stream) stream.getTracks().forEach((track) => track.stop());
  };
}

async function beginCapture() {
  if (!stream || !ws || ws.readyState !== WebSocket.OPEN)
    throw new Error("Microphone stream or WebSocket is not available");
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) throw new Error("This browser does not support AudioContext");
  if (!window.AudioWorkletNode) throw new Error("This browser does not support AudioWorklet");

  ctx = new AudioContextClass({ sampleRate: TARGET_RATE });
  if (ctx.state === "suspended") await ctx.resume();
  await ctx.audioWorklet.addModule("/static/worklet.js");
  const src = ctx.createMediaStreamSource(stream);
  node = new AudioWorkletNode(ctx, "pcm-forwarder");
  node.port.onmessage = (e) => {
    if (ws && ws.readyState === WebSocket.OPEN)
      ws.send(toPCM16(e.data, ctx.sampleRate));
  };
  src.connect(node);
  // Keep the worklet graph active without sending microphone audio to speakers.
  const silentGain = ctx.createGain();
  silentGain.gain.value = 0;
  node.connect(silentGain);
  silentGain.connect(ctx.destination);

  recording = true;
  $("rec").disabled = false;
  $("rec").classList.add("recording");
  $("rec-label").textContent = "Stop & save";
  setStatus("Listening… speak now.");
}

function stopCapture() {
  recording = false;
  try { node && node.disconnect(); } catch {}
  stream && stream.getTracks().forEach((t) => t.stop());
  ctx && ctx.close().catch(() => {});
}
function finishUI() {
  stopCapture();
  $("rec").classList.remove("recording");
  $("rec-label").textContent = "Start speaking";
  $("rec").disabled = false;
  renderLive();
}
function stopRecording() {
  stopCapture();
  $("rec").disabled = true;
  setStatus("Finishing up…");
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stop" }));
}

$("rec").onclick = () => (recording ? stopRecording() : startRecording());

load();
