/* Caseloop chat widget. One script tag, no dependencies, styles isolated in a shadow root.
 *
 *   <script src="https://your-caseloop-host/widget.js"
 *           data-key="pk_..."            required: the business's widget key
 *           data-name="Acme Lamps"       header title (default "Support")
 *           data-label="Need help?"      launcher text (default "Help")
 *           data-color="#1f4d3a"         accent (default a deep green)
 *           data-base="https://host"     API base (default: the script's own origin)
 *   ></script>
 */
(() => {
  const script = document.currentScript;
  if (!script) return;
  const KEY = script.dataset.key;
  if (!KEY) { console.warn("[caseloop] data-key is required"); return; }
  const BASE = (script.dataset.base || new URL(script.src, location.href).origin).replace(/\/$/, "");
  const NAME = script.dataset.name || "Support";
  const LABEL = script.dataset.label || "Help";
  const ACCENT = script.dataset.color || "#1f4d3a";
  const STORE = `caseloop:${KEY}`;
  const POLL_MS = 3000;
  const MAX_FILES = 5, MAX_BYTES = 8 * 1024 * 1024;
  const FILE_TYPES = /^(image\/(jpeg|png|gif|webp|heic|heif)|application\/pdf)$/;

  const STATE_COPY = {
    new: "Got it. Looking into this now.",
    gathering: "Looking into this now.",
    awaiting_customer: "Waiting on you. Reply here whenever you're ready.",
    resolved: "Resolved. Reply here if anything else comes up.",
    escalated: "A person on the team has this now. They'll reply here.",
    abandoned: "This chat went quiet. Send a message to pick it back up.",
  };

  const host = document.createElement("div");
  host.id = "caseloop-widget";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
  <style>
    :host { all: initial; }
    * { box-sizing: border-box; }
    .cl {
      --accent: ${ACCENT};
      --ink: #15201b;
      --muted: #5f6b66;
      --line: #dfe5e2;
      --paper: #ffffff;
      --wash: #f3f6f4;
      --radius: 14px;
      --ease-out: cubic-bezier(0.23, 1, 0.32, 1);
      position: fixed; right: 20px; bottom: 20px; z-index: 2147483000;
      font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      color: var(--ink);
      -webkit-font-smoothing: antialiased;
    }
    button { font: inherit; cursor: pointer; border: 0; background: none; color: inherit; }
    button:focus-visible, textarea:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

    .launch {
      display: inline-flex; align-items: center; gap: 10px;
      height: 48px; padding: 0 18px 0 14px; border-radius: 999px;
      background: var(--accent); color: #fff; font-weight: 600;
      box-shadow: 0 6px 20px rgba(21, 32, 27, .18);
      transition: transform 160ms var(--ease-out);
    }
    .launch:active { transform: scale(.97); }
    .launch svg { width: 20px; height: 20px; }
    .launch .dot { width: 8px; height: 8px; border-radius: 50%; background: #9ff0c2; display: none; }
    .launch.has-news .dot { display: block; }

    .panel {
      position: absolute; right: 0; bottom: 60px;
      width: min(380px, calc(100vw - 40px)); height: min(600px, calc(100vh - 100px));
      display: flex; flex-direction: column;
      background: var(--paper); border: 1px solid var(--line); border-radius: var(--radius);
      box-shadow: 0 24px 60px rgba(21, 32, 27, .22);
      transform-origin: bottom right;
      opacity: 0; transform: scale(.96) translateY(6px); pointer-events: none;
      transition: opacity 180ms var(--ease-out), transform 180ms var(--ease-out);
    }
    .panel.open { opacity: 1; transform: none; pointer-events: auto; }

    .view { flex: 1; min-height: 0; display: flex; flex-direction: column; }
    .head { display: flex; align-items: flex-start; gap: 12px; padding: 16px 16px 12px; border-bottom: 1px solid var(--line); }
    .head .title { font-weight: 650; font-size: 16px; }
    .head .status { color: var(--muted); font-size: 13px; margin-top: 2px; }
    .head .close { margin-left: auto; width: 32px; height: 32px; border-radius: 8px; display: grid; place-items: center; color: var(--muted); transition: transform 160ms var(--ease-out), background 160ms ease; }
    .head .close:active { transform: scale(.94); }
    @media (hover: hover) { .head .close:hover { background: var(--wash); } }

    .body { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px; background: var(--wash); }
    .msg { max-width: 82%; padding: 10px 13px; border-radius: 16px; white-space: pre-wrap; word-wrap: break-word;
           opacity: 1; transform: none; transition: opacity 200ms var(--ease-out), transform 200ms var(--ease-out); }
    .msg.enter { opacity: 0; transform: translateY(6px); }
    .msg.me { align-self: flex-end; background: var(--accent); color: #fff; border-bottom-right-radius: 6px; }
    .msg.them { align-self: flex-start; background: var(--paper); border: 1px solid var(--line); border-bottom-left-radius: 6px; }
    .msg.human { border-color: var(--accent); }
    .msg .who { display: block; font-size: 12px; color: var(--muted); margin-bottom: 3px; }
    .msg.me .who { color: rgba(255,255,255,.75); }
    .notice { align-self: center; font-size: 12.5px; color: var(--muted); text-align: center; padding: 4px 8px; }
    .msg .files { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
    .msg .who + .files { margin-top: 2px; }
    .msg .thumb { display: block; width: 96px; height: 96px; border-radius: 10px; overflow: hidden; background: rgba(21,32,27,.08); }
    .msg .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .msg .file { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; padding: 6px 10px; border-radius: 10px; font-size: 13px;
                 background: rgba(21,32,27,.06); color: inherit; text-decoration: none; }
    .msg.me .file { background: rgba(255,255,255,.18); }
    .msg .file svg { width: 14px; height: 14px; flex-shrink: 0; }
    .msg .file span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .typing { align-self: flex-start; display: inline-flex; align-items: center; gap: 8px; padding: 8px 13px; border-radius: 16px; border-bottom-left-radius: 6px;
              background: var(--paper); border: 1px solid var(--line); color: var(--muted); font-size: 13px;
              opacity: 1; transition: opacity 200ms var(--ease-out); }
    .typing.enter { opacity: 0; }
    .typing .dots { display: inline-flex; gap: 3px; }
    .typing .dots i { width: 5px; height: 5px; border-radius: 50%; background: currentColor; opacity: .35; animation: cl-dot 1.2s infinite var(--ease-out); }
    .typing .dots i:nth-child(2) { animation-delay: 160ms; }
    .typing .dots i:nth-child(3) { animation-delay: 320ms; }
    @keyframes cl-dot { 0%, 60%, 100% { opacity: .35; transform: translateY(0); } 30% { opacity: 1; transform: translateY(-2px); } }

    form.start { display: flex; flex-direction: column; gap: 10px; padding: 18px 16px; }
    form.start p { margin: 0 0 4px; color: var(--muted); }
    .field { display: flex; flex-direction: column; gap: 4px; }
    .field label { font-size: 13px; color: var(--muted); }
    input, textarea { font: inherit; color: var(--ink); background: var(--paper); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; width: 100%; }
    textarea { resize: none; min-height: 88px; }

    .compose { display: flex; flex-direction: column; gap: 8px; padding: 12px; border-top: 1px solid var(--line); background: var(--paper); border-radius: 0 0 var(--radius) var(--radius); }
    .compose .row { display: flex; gap: 8px; }
    .compose textarea { min-height: 44px; max-height: 120px; }
    .compose input[type=file] { display: none; }
    .attach { width: 44px; height: 44px; border-radius: 10px; display: grid; place-items: center; color: var(--muted); flex-shrink: 0;
              transition: transform 160ms var(--ease-out), background 160ms ease, color 160ms ease; }
    .attach svg { width: 20px; height: 20px; }
    .attach:active { transform: scale(.94); }
    .attach[disabled] { opacity: .55; cursor: default; }
    @media (hover: hover) { .attach:hover { background: var(--wash); color: var(--ink); } }
    .pending { display: none; flex-wrap: wrap; gap: 8px; }
    .pending.has { display: flex; }
    .pending .item { position: relative; width: 56px; height: 56px; border-radius: 10px; overflow: hidden; background: var(--wash); border: 1px solid var(--line);
                     opacity: 1; transform: none; transition: opacity 160ms var(--ease-out), transform 160ms var(--ease-out); }
    .pending .item.enter { opacity: 0; transform: scale(.9); }
    .pending .item img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .pending .item .doc { width: 100%; height: 100%; display: grid; place-items: center; font-size: 10px; font-weight: 600; color: var(--muted); letter-spacing: .04em; }
    .pending .remove { position: absolute; top: 3px; right: 3px; width: 18px; height: 18px; border-radius: 50%; display: grid; place-items: center;
                       background: rgba(21,32,27,.75); color: #fff; }
    .pending .remove svg { width: 9px; height: 9px; }
    .send, .primary {
      height: 44px; padding: 0 16px; border-radius: 10px; background: var(--accent); color: #fff; font-weight: 600;
      transition: transform 160ms var(--ease-out), opacity 160ms ease; flex-shrink: 0;
    }
    .send:active, .primary:active { transform: scale(.97); }
    .send[disabled], .primary[disabled] { opacity: .55; cursor: default; }
    .error { color: #a3341f; font-size: 13px; }

    @media (prefers-reduced-motion: reduce) {
      .panel, .msg, .launch, .send, .primary, .close, .attach, .typing, .pending .item { transition: opacity 150ms ease; transform: none !important; }
      .typing .dots i { animation: none; opacity: .7; }
    }
  </style>
  <div class="cl">
    <div class="panel" role="dialog" aria-label="${NAME} chat" aria-hidden="true">
      <div class="head">
        <div>
          <div class="title">${NAME}</div>
          <div class="status">Tell us what went wrong. We'll sort it out here.</div>
        </div>
        <button class="close" aria-label="Close chat">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 3l10 10M13 3L3 13"/></svg>
        </button>
      </div>
      <div class="view"></div>
    </div>
    <button class="launch" aria-expanded="false">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8 8 0 0 1-8 8H7l-4 3V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z"/></svg>
      <span>${LABEL}</span><span class="dot"></span>
    </button>
  </div>`;

  const $ = (sel) => root.querySelector(sel);
  const panel = $(".panel"), launch = $(".launch"), view = $(".view"), statusEl = $(".status");
  let session = load();
  let open = false, pollTimer = null, stream = null, lastSeen = null, seenIds = new Set();
  let curState = null, lastDirection = null, pending = [];
  let restoring = false, pollVersion = 0, pollPending = false;

  function load() { try { return JSON.parse(localStorage.getItem(STORE) || "null"); } catch { return null; } }
  function save(s) {
    session = s;
    try { if (s) localStorage.setItem(STORE, JSON.stringify(s)); else localStorage.removeItem(STORE); }
    catch { /* Keep the current chat usable when the browser blocks storage. */ }
  }

  async function api(path, opts = {}) {
    const json = !(opts.body instanceof FormData);
    const res = await fetch(BASE + path, { ...opts, headers: { ...(json ? { "Content-Type": "application/json" } : {}), "X-Widget-Key": KEY, ...(opts.headers || {}) } });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const error = new Error((body.error && body.error.message) || `Request failed (${res.status})`);
      error.status = res.status;
      throw error;
    }
    return body;
  }

  function renderStart() {
    stopLive();
    clearPending();
    seenIds = new Set(); lastSeen = null; lastDirection = null; curState = null;
    statusEl.textContent = "Tell us what went wrong. We'll sort it out here.";
    view.innerHTML = `
      <form class="start" novalidate>
        <p>Leave your email so we can keep helping even if you close this tab.</p>
        <div class="field"><label for="cl-email">Email</label><input id="cl-email" name="email" type="email" required autocomplete="email" /></div>
        <div class="field"><label for="cl-name">Name</label><input id="cl-name" name="name" type="text" autocomplete="name" /></div>
        <div class="field"><label for="cl-msg">What happened?</label><textarea id="cl-msg" name="message" required placeholder="Order number, what arrived, what you'd like us to do…"></textarea></div>
        <div class="error" aria-live="polite"></div>
        <button class="primary" type="submit">Start chat</button>
      </form>`;
    const form = $("form.start"), err = $(".error");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (form.querySelector(".primary").disabled) return;
      const data = Object.fromEntries(new FormData(form).entries());
      if (!data.email || !data.message) { err.textContent = "Email and a message are needed to start."; return; }
      form.querySelector(".primary").disabled = true;
      try {
        const out = await api("/v1/chat/sessions", { method: "POST", body: JSON.stringify({ email: data.email, name: data.name || null, message: data.message, page_url: location.href }) });
        save({ token: out.token, case_id: out.case_id });
        renderChat(out.messages, out.state);
      } catch (ex) {
        err.textContent = ex.message;
        form.querySelector(".primary").disabled = false;
      }
    });
    setTimeout(() => open && $("#cl-email") && $("#cl-email").focus(), 50);
  }

  function renderChat(messages, state) {
    clearPending();
    view.innerHTML = `
      <div class="body" aria-live="polite"></div>
      <form class="compose">
        <div class="pending" aria-label="Files to send"></div>
        <div class="row">
          <input type="file" accept="image/*,application/pdf" multiple aria-hidden="true" tabindex="-1" />
          <button class="attach" type="button" aria-label="Attach photos" title="Attach photos (up to 5)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21.4 11.1l-8.8 8.8a5.5 5.5 0 0 1-7.8-7.8l9.2-9.2a3.7 3.7 0 0 1 5.2 5.2l-9.2 9.2a1.8 1.8 0 0 1-2.6-2.6l8.5-8.5"/></svg>
          </button>
          <textarea rows="1" placeholder="Write a reply…" aria-label="Your message"></textarea>
          <button class="send" type="submit">Send</button>
        </div>
      </form>`;
    seenIds = new Set(); lastSeen = null; lastDirection = null; pending = [];
    appendMessages(messages, false);
    setStatus(state);
    const form = $("form.compose"), ta = form.querySelector("textarea"), btn = form.querySelector(".send");
    const picker = form.querySelector("input[type=file]"), attach = form.querySelector(".attach");
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } });
    ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 120) + "px"; });
    attach.addEventListener("click", () => picker.click());
    picker.addEventListener("change", () => { addPending(Array.from(picker.files || [])); picker.value = ""; ta.focus(); });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (btn.disabled || !session) return;
      const text = ta.value.trim();
      const files = pending.slice();
      const token = session.token;
      if (!text && !files.length) return;
      btn.disabled = true; attach.disabled = true; ta.value = ""; ta.style.height = "auto";
      try {
        let out;
        if (files.length) {
          const fd = new FormData();
          for (const p of files) fd.append("files", p.file, p.file.name);
          if (text) fd.append("caption", text);
          out = await api(`/v1/chat/sessions/${token}/attachments`, { method: "POST", body: fd });
        } else {
          out = await api(`/v1/chat/sessions/${token}/messages`, { method: "POST", body: JSON.stringify({ text }) });
        }
        if (!session || session.token !== token || $("form.compose") !== form) return;
        for (const file of files) removePending(file);
        appendMessages(out.messages, true); setStatus(out.state);
      } catch (ex) {
        if (!session || session.token !== token || $("form.compose") !== form) return;
        if (ex.status === 404) { save(null); renderStart(); return; }
        if (text) ta.value = text + (ta.value ? "\n" + ta.value : "");
        notice(ex.message);
      } finally { btn.disabled = false; attach.disabled = false; if (open && $("form.compose") === form) ta.focus(); }
    });
    startLive();
    setTimeout(() => open && ta.focus(), 50);
  }

  function addPending(files) {
    const strip = $(".pending");
    if (!strip) return;
    for (const file of files) {
      if (pending.length >= MAX_FILES) { notice(`You can send up to ${MAX_FILES} files at a time.`); break; }
      if (!FILE_TYPES.test(file.type)) { notice(`${file.name}: only photos and PDFs can be sent.`); continue; }
      if (file.size > MAX_BYTES) { notice(`${file.name} is over 8 MB.`); continue; }
      const item = document.createElement("div"); item.className = "item enter";
      const entry = { file, url: null, el: item };
      if (file.type.startsWith("image/")) {
        entry.url = URL.createObjectURL(file);
        const img = document.createElement("img"); img.src = entry.url; img.alt = file.name; item.appendChild(img);
      } else {
        const doc = document.createElement("div"); doc.className = "doc"; doc.textContent = "PDF"; doc.title = file.name; item.appendChild(doc);
      }
      const rm = document.createElement("button"); rm.type = "button"; rm.className = "remove"; rm.setAttribute("aria-label", `Remove ${file.name}`);
      rm.innerHTML = '<svg viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M2 2l6 6M8 2L2 8"/></svg>';
      rm.addEventListener("click", () => removePending(entry));
      item.appendChild(rm);
      strip.appendChild(item);
      pending.push(entry);
      requestAnimationFrame(() => requestAnimationFrame(() => item.classList.remove("enter")));
    }
    strip.classList.toggle("has", pending.length > 0);
  }
  function removePending(entry) {
    pending = pending.filter((p) => p !== entry);
    if (entry.url) URL.revokeObjectURL(entry.url);
    entry.el.remove();
    const strip = $(".pending"); if (strip) strip.classList.toggle("has", pending.length > 0);
  }
  function clearPending() { for (const p of pending.slice()) removePending(p); }

  function appendMessages(list, animate) {
    const body = $(".body");
    let added = 0;
    for (const m of list) {
      if (seenIds.has(m.id)) continue;
      seenIds.add(m.id);
      const mine = m.direction === "inbound";
      if (!lastSeen || m.at > lastSeen) lastSeen = m.at;
      lastDirection = m.direction;
      if (!mine) added++;
      if (!body) continue;
      const el = document.createElement("div");
      el.className = `msg ${mine ? "me" : "them"}${m.sender === "human" ? " human" : ""}${animate ? " enter" : ""}`;
      const who = document.createElement("span"); who.className = "who";
      who.textContent = mine ? "You" : (m.sender === "human" ? `${NAME} team` : NAME);
      el.appendChild(who);
      const files = m.attachments || [];
      const isSummary = files.length && /^Sent (a|\d+) (photo|file)s?$/.test(m.text);
      if (!isSummary) el.appendChild(document.createTextNode(m.text));
      if (files.length) el.appendChild(renderFiles(m, files));
      body.appendChild(el);
      if (animate) requestAnimationFrame(() => requestAnimationFrame(() => el.classList.remove("enter")));
    }
    updateTyping();
    if (body) body.scrollTop = body.scrollHeight;
    if (added && !open) launch.classList.add("has-news");
  }

  function renderFiles(m, files) {
    const wrap = document.createElement("div"); wrap.className = "files";
    for (const a of files) {
      const url = `${BASE}/v1/chat/sessions/${session.token}/attachments/${m.id}/${a.index}`;
      const link = document.createElement("a"); link.href = url; link.target = "_blank"; link.rel = "noopener";
      if ((a.content_type || "").startsWith("image/")) {
        link.className = "thumb"; link.setAttribute("aria-label", `Open ${a.filename}`);
        const img = document.createElement("img"); img.src = url; img.alt = a.filename; img.loading = "lazy";
        img.addEventListener("load", () => { const body = $(".body"); if (body) body.scrollTop = body.scrollHeight; });
        link.appendChild(img);
      } else {
        link.className = "file";
        link.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>';
        const name = document.createElement("span"); name.textContent = a.filename; link.appendChild(name);
      }
      wrap.appendChild(link);
    }
    return wrap;
  }

  function updateTyping() {
    const body = $(".body");
    if (!body) return;
    const existing = body.querySelector(".typing");
    const show = (curState === "gathering" || curState === "new") && lastDirection === "inbound";
    if (!show) { if (existing) existing.remove(); return; }
    if (existing) { body.appendChild(existing); return; }
    const el = document.createElement("div"); el.className = "typing enter"; el.setAttribute("aria-label", `${NAME} is typing`);
    el.innerHTML = `<span>${NAME} is typing</span><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>`;
    body.appendChild(el);
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.remove("enter")));
    body.scrollTop = body.scrollHeight;
  }

  function notice(text) { const body = $(".body"); if (!body) return; const n = document.createElement("div"); n.className = "notice"; n.textContent = text; body.appendChild(n); body.scrollTop = body.scrollHeight; }
  function setStatus(state) { curState = state; statusEl.textContent = STATE_COPY[state] || "We're on it."; updateTyping(); }

  async function poll() {
    if (!session || pollPending) return;
    const token = session.token, version = pollVersion;
    pollPending = true;
    try {
      const q = lastSeen ? `?after=${encodeURIComponent(lastSeen)}` : "";
      const out = await api(`/v1/chat/sessions/${token}/messages${q}`);
      if (version !== pollVersion || !session || session.token !== token) return;
      appendMessages(out.messages, true); setStatus(out.state);
    } catch (ex) {
      if (version === pollVersion && session && session.token === token && ex.status === 404) { save(null); renderStart(); }
    } finally { if (version === pollVersion) pollPending = false; }
  }
  function startPolling() { stopPolling(); if (!session) return; poll(); pollTimer = setInterval(() => { if (!document.hidden) poll(); }, POLL_MS); }
  function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; pollVersion++; pollPending = false; }

  // Live updates: server-sent events when the browser has them, 3 s polling otherwise.
  function startLive() {
    stopLive();
    if (!session) return;
    if (!open || typeof EventSource === "undefined") { startPolling(); return; }
    const q = lastSeen ? `?after=${encodeURIComponent(lastSeen)}` : "";
    let es;
    try { es = new EventSource(`${BASE}/v1/chat/sessions/${session.token}/stream${q}`); } catch { startPolling(); return; }
    stream = es;
    es.addEventListener("message", (e) => { if (stream !== es) return; try { appendMessages([JSON.parse(e.data)], true); } catch { /* ignore a bad frame */ } });
    es.addEventListener("state", (e) => {
      if (stream !== es) return;
      try {
        const { state } = JSON.parse(e.data);
        if (state === "closed") { save(null); renderStart(); return; }
        setStatus(state);
      } catch { /* ignore a bad frame */ }
    });
    es.addEventListener("error", () => {
      // The server ends every stream after ten minutes; EventSource reconnects on its own after the
      // `retry:` delay it sent, and seenIds drops anything re-sent. Only a stream the browser gave up
      // on (404, 429, no SSE support) falls back to polling.
      if (es.readyState === EventSource.CLOSED && stream === es) { stream = null; startPolling(); }
    });
  }
  function stopLive() { stopPolling(); if (stream) { stream.close(); stream = null; } }

  async function resume() {
    if (!session) { if (!$("form.start")) renderStart(); return; }
    if ($("form.compose")) { startLive(); return; }
    if (restoring) return;
    stopLive();
    restoring = true;
    const token = session.token;
    try {
      const out = await api(`/v1/chat/sessions/${token}/messages`);
      if (!session || session.token !== token) return;
      renderChat(out.messages, out.state);
    } catch (ex) {
      if (!session || session.token !== token) return;
      if (ex.status === 404) { save(null); renderStart(); return; }
      view.innerHTML = '<form class="start retry"><div class="error" aria-live="polite"></div><button class="primary" type="submit">Retry</button></form>';
      $(".error").textContent = ex.message;
      $("form.retry").addEventListener("submit", (e) => { e.preventDefault(); return resume(); });
    } finally { restoring = false; }
  }

  function setOpen(next) {
    open = next;
    panel.classList.toggle("open", open);
    panel.setAttribute("aria-hidden", String(!open));
    launch.setAttribute("aria-expanded", String(open));
    if (open) { launch.classList.remove("has-news"); resume(); } else { startLive(); }
  }
  launch.addEventListener("click", () => setOpen(!open));
  $(".close").addEventListener("click", () => setOpen(false));
  root.addEventListener("keydown", (e) => { if (e.key === "Escape" && open) setOpen(false); });

  document.body.appendChild(host);
  if (session) { startPolling(); }
  window.addEventListener("pagehide", stopLive);
  window.addEventListener("pageshow", (e) => { if (e.persisted && session) startLive(); });
})();
