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

    form.start { display: flex; flex-direction: column; gap: 10px; padding: 18px 16px; }
    form.start p { margin: 0 0 4px; color: var(--muted); }
    .field { display: flex; flex-direction: column; gap: 4px; }
    .field label { font-size: 13px; color: var(--muted); }
    input, textarea { font: inherit; color: var(--ink); background: var(--paper); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; width: 100%; }
    textarea { resize: none; min-height: 88px; }

    .compose { display: flex; gap: 8px; padding: 12px; border-top: 1px solid var(--line); background: var(--paper); border-radius: 0 0 var(--radius) var(--radius); }
    .compose textarea { min-height: 44px; max-height: 120px; }
    .send, .primary {
      height: 44px; padding: 0 16px; border-radius: 10px; background: var(--accent); color: #fff; font-weight: 600;
      transition: transform 160ms var(--ease-out), opacity 160ms ease; flex-shrink: 0;
    }
    .send:active, .primary:active { transform: scale(.97); }
    .send[disabled], .primary[disabled] { opacity: .55; cursor: default; }
    .error { color: #a3341f; font-size: 13px; }

    @media (prefers-reduced-motion: reduce) {
      .panel, .msg, .launch, .send, .primary, .close { transition: opacity 150ms ease; transform: none !important; }
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
  let open = false, pollTimer = null, lastSeen = null, seenIds = new Set();

  function load() { try { return JSON.parse(localStorage.getItem(STORE) || "null"); } catch { return null; } }
  function save(s) { session = s; if (s) localStorage.setItem(STORE, JSON.stringify(s)); else localStorage.removeItem(STORE); }

  async function api(path, opts = {}) {
    const res = await fetch(BASE + path, { ...opts, headers: { "Content-Type": "application/json", "X-Widget-Key": KEY, ...(opts.headers || {}) } });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((body.error && body.error.message) || `Request failed (${res.status})`);
    return body;
  }

  function renderStart() {
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
    setTimeout(() => $("#cl-email") && $("#cl-email").focus(), 50);
  }

  function renderChat(messages, state) {
    view.innerHTML = `
      <div class="body" aria-live="polite"></div>
      <form class="compose">
        <textarea rows="1" placeholder="Write a reply…" aria-label="Your message"></textarea>
        <button class="send" type="submit">Send</button>
      </form>`;
    seenIds = new Set(); lastSeen = null;
    appendMessages(messages, false);
    setStatus(state);
    const form = $("form.compose"), ta = form.querySelector("textarea"), btn = form.querySelector(".send");
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
    ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 120) + "px"; });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = ta.value.trim();
      if (!text) return;
      btn.disabled = true; ta.value = ""; ta.style.height = "auto";
      try {
        const out = await api(`/v1/chat/sessions/${session.token}/messages`, { method: "POST", body: JSON.stringify({ text }) });
        appendMessages(out.messages, true); setStatus(out.state);
      } catch (ex) {
        if (/not found/i.test(ex.message)) { save(null); renderStart(); return; }
        notice(ex.message);
      } finally { btn.disabled = false; ta.focus(); }
    });
    startPolling();
    setTimeout(() => ta.focus(), 50);
  }

  function appendMessages(list, animate) {
    const body = $(".body");
    if (!body) return;
    let added = 0;
    for (const m of list) {
      if (seenIds.has(m.id)) continue;
      seenIds.add(m.id);
      const el = document.createElement("div");
      const mine = m.direction === "inbound";
      el.className = `msg ${mine ? "me" : "them"}${m.sender === "human" ? " human" : ""}${animate ? " enter" : ""}`;
      const who = document.createElement("span"); who.className = "who";
      who.textContent = mine ? "You" : (m.sender === "human" ? `${NAME} team` : NAME);
      el.appendChild(who); el.appendChild(document.createTextNode(m.text));
      body.appendChild(el);
      if (animate) requestAnimationFrame(() => requestAnimationFrame(() => el.classList.remove("enter")));
      if (!lastSeen || m.at > lastSeen) lastSeen = m.at;
      if (!mine) added++;
    }
    body.scrollTop = body.scrollHeight;
    if (added && !open) launch.classList.add("has-news");
  }

  function notice(text) { const body = $(".body"); if (!body) return; const n = document.createElement("div"); n.className = "notice"; n.textContent = text; body.appendChild(n); body.scrollTop = body.scrollHeight; }
  function setStatus(state) { statusEl.textContent = STATE_COPY[state] || "We're on it."; }

  async function poll() {
    if (!session) return;
    try {
      const q = lastSeen ? `?after=${encodeURIComponent(lastSeen)}` : "";
      const out = await api(`/v1/chat/sessions/${session.token}/messages${q}`);
      appendMessages(out.messages, true); setStatus(out.state);
    } catch (ex) {
      if (/not found/i.test(ex.message)) { stopPolling(); save(null); if (open) renderStart(); }
    }
  }
  function startPolling() { stopPolling(); poll(); pollTimer = setInterval(() => { if (!document.hidden) poll(); }, POLL_MS); }
  function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

  async function resume() {
    if (!session) { renderStart(); return; }
    try {
      const out = await api(`/v1/chat/sessions/${session.token}/messages`);
      renderChat(out.messages, out.state);
    } catch { save(null); renderStart(); }
  }

  function setOpen(next) {
    open = next;
    panel.classList.toggle("open", open);
    panel.setAttribute("aria-hidden", String(!open));
    launch.setAttribute("aria-expanded", String(open));
    if (open) { launch.classList.remove("has-news"); resume(); } else { stopPolling(); }
  }
  launch.addEventListener("click", () => setOpen(!open));
  $(".close").addEventListener("click", () => setOpen(false));
  root.addEventListener("keydown", (e) => { if (e.key === "Escape" && open) setOpen(false); });

  document.body.appendChild(host);
  if (session) { startPolling(); }
})();
