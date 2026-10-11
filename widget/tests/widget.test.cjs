const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

// Small DOM adapter for the widget's browser events; no build or external test dependency.
class Element {
  constructor(tag = "div") {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.className = "";
    this.value = "";
    this.style = {};
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name),
      add: (name) => { if (!this.classList.contains(name)) this.className += ` ${name}`; },
      remove: (name) => { this.className = this.className.split(/\s+/).filter((c) => c !== name).join(" "); },
      toggle: (name, show) => {
        if (show ?? !this.classList.contains(name)) this.classList.add(name);
        else this.classList.remove(name);
      },
    };
  }
  set innerHTML(html) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    const stack = [this];
    for (const token of html.replace(/<style>[\s\S]*?<\/style>/g, "").match(/<[^>]*>|[^<]+/g) || []) {
      if (token.startsWith("</")) { stack.pop(); continue; }
      if (!token.startsWith("<")) continue;
      const tag = token.match(/^<([\w-]+)/)?.[1];
      if (!tag) continue;
      const el = new Element(tag);
      for (const match of token.matchAll(/([\w-]+)="([^"]*)"/g)) el.setAttribute(match[1], match[2]);
      stack.at(-1).appendChild(el);
      if (!token.endsWith("/>") && !["input", "img", "br"].includes(tag)) stack.push(el);
    }
  }
  setAttribute(name, value) {
    this.attributes[name] = value;
    if (name === "class") this.className = value;
  }
  matches(selector) {
    if (selector.startsWith("#")) return this.attributes.id === selector.slice(1);
    const [, tag, cls, attr, value] = selector.match(/^(\w+)?(?:\.([\w-]+))?(?:\[([\w-]+)=([^\]]+)\])?$/) || [];
    return (!tag || tag === this.tag) && (!cls || this.classList.contains(cls)) && (!attr || this.attributes[attr] === value);
  }
  querySelector(selector) {
    for (const child of this.children) {
      if (child.matches(selector)) return child;
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }
  appendChild(child) {
    child.remove();
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
    this.parentNode = null;
  }
  attachShadow() { this.shadowRoot = new Element("shadow"); return this.shadowRoot; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  async dispatch(type, event = {}) {
    await Promise.all((this.listeners[type] || []).map((listener) => listener({ preventDefault() {}, ...event })));
  }
  click() { return this.dispatch("click"); }
  requestSubmit() { return this.dispatch("submit"); }
  focus() {}
}

function mount({ session = null, storageFails = false, eventSource = true } = {}) {
  const body = new Element("body");
  const requests = [], intervals = new Map(), streams = [], revoked = [];
  const stored = new Map(session ? [["caseloop:pk_test", JSON.stringify(session)]] : []);
  const window = new Element("window");
  const document = { body, hidden: false, currentScript: { dataset: { key: "pk_test" }, src: "https://support.example/widget.js" }, createElement: (tag) => new Element(tag), createTextNode: (text) => Object.assign(new Element("text"), { textContent: text }) };
  class TestFormData {
    constructor(form) {
      this.values = [];
      const visit = (node) => {
        if (node.attributes.name) this.values.push([node.attributes.name, node.value]);
        node.children.forEach(visit);
      };
      if (form) visit(form);
    }
    append(name, value) { this.values.push([name, value]); }
    entries() { return this.values.values(); }
  }
  class TestURL extends URL {
    static createObjectURL(file) { return `blob:${file.name}`; }
    static revokeObjectURL(url) { revoked.push(url); }
  }
  class TestEventSource extends Element {
    static CLOSED = 2;
    constructor(url) { super("stream"); this.url = url; this.readyState = 1; streams.push(this); }
    close() { this.readyState = TestEventSource.CLOSED; }
  }
  let timerId = 0;
  const context = {
    document, window, console, URL: TestURL, FormData: TestFormData,
    location: { href: "https://shop.example/" },
    EventSource: eventSource ? TestEventSource : undefined,
    localStorage: {
      getItem: (key) => stored.get(key) || null,
      setItem: (key, value) => { if (storageFails) throw new Error("Storage blocked"); stored.set(key, value); },
      removeItem: (key) => { if (storageFails) throw new Error("Storage blocked"); stored.delete(key); },
    },
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({
      url, options, reject,
      respond: (data, status = 200) => resolve({ ok: status < 400, status, json: async () => data }),
    })),
    setInterval: (callback) => { intervals.set(++timerId, callback); return timerId; },
    clearInterval: (id) => intervals.delete(id),
    setTimeout: (callback) => { callback(); },
    requestAnimationFrame: (callback) => callback(),
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../widget.js"), "utf8"), context);
  const root = body.children[0].shadowRoot;
  return { root, requests, intervals, streams, revoked, stored, window, $: (selector) => root.querySelector(selector) };
}

const message = (id, direction = "inbound") => ({ id, direction, sender: direction === "inbound" ? "customer" : "agent", text: id, at: `2026-10-11T00:00:0${id.length}Z`, attachments: [] });
const initial = { token: "session_token", case_id: "case_id", state: "new", messages: [message("hi")] };
const flush = () => new Promise((resolve) => setImmediate(resolve));

async function startChat(options) {
  const app = mount(options);
  await app.$(".launch").click();
  app.$("#cl-email").value = "pat@example.com";
  app.$("#cl-msg").value = "hi";
  const submitted = app.$("form.start").requestSubmit();
  app.requests[0].respond(initial);
  await submitted;
  return app;
}

async function pickPhoto(app, name = "photo.png") {
  const picker = app.$("input[type=file]");
  picker.files = [{ name, type: "image/png", size: 50 }];
  await picker.dispatch("change");
}

test("closing and reopening preserves the unfinished reply and photos", async () => {
  const app = await startChat();
  const composer = app.$("form.compose");
  composer.querySelector("textarea").value = "Here is the damage";
  await pickPhoto(app);
  await app.$(".close").click();
  await app.$(".launch").click();
  for (const request of app.requests.slice(1)) request.respond(initial);
  await flush();
  assert.ok(app.$("form.compose") === composer, "reopening should retain the composer");
  assert.equal(app.$("form.compose").querySelector("textarea").value, "Here is the damage");
  assert.ok(app.$(".pending").querySelector(".item"));
  assert.deepEqual(app.revoked, []);
});

test("a pending upload cannot be submitted again with Enter", async () => {
  const app = await startChat();
  await pickPhoto(app);
  const submit = app.$("form.compose").requestSubmit();
  await app.$("textarea").dispatch("keydown", { key: "Enter", shiftKey: false });
  assert.equal(app.requests.filter((r) => r.url.endsWith("/attachments")).length, 1);
  app.requests.at(-1).respond({ state: "gathering", messages: [message("photo")] });
  await submit;
});

test("a transient restore failure keeps the existing session and offers retry", async () => {
  const app = mount({ session: initial });
  app.requests[0].respond({ state: "new", messages: initial.messages });
  await flush();
  await app.$(".launch").click();
  app.requests.at(-1).respond({ error: { message: "Temporarily unavailable" } }, 503);
  await flush();
  assert.equal(JSON.parse(app.stored.get("caseloop:pk_test")).token, initial.token);
  assert.ok(app.$("form.retry"));
  const retry = app.$("form.retry").requestSubmit();
  app.requests.at(-1).respond({ state: "new", messages: initial.messages });
  await retry;
  await flush();
  assert.ok(app.$("form.compose"));
});

test("new replies notify while the chat is closed", async () => {
  const app = await startChat();
  await app.$(".close").click();
  assert.equal(app.intervals.size, 1);
  app.requests.at(-1).respond({ state: "awaiting_customer", messages: [message("reply", "outbound")] });
  await flush();
  assert.ok(app.$(".launch").classList.contains("has-news"));
});

test("background polling can notify before the panel has first opened", async () => {
  const app = mount({ session: initial });
  app.requests[0].respond({ state: "awaiting_customer", messages: [message("reply", "outbound")] });
  await flush();
  assert.ok(app.$(".launch").classList.contains("has-news"));
});

test("blocked local storage does not prevent the new chat from opening", async () => {
  const app = await startChat({ storageFails: true });
  assert.ok(app.$("form.compose"));
});

test("failed sends preserve the new draft typed while waiting", async () => {
  const app = await startChat();
  const textarea = app.$("textarea");
  textarea.value = "First reply";
  const submit = app.$("form.compose").requestSubmit();
  textarea.value = "Another detail";
  app.requests.at(-1).respond({ error: { message: "Please try again" } }, 503);
  await submit;
  assert.match(textarea.value, /First reply/);
  assert.match(textarea.value, /Another detail/);
});

test("closing and reopening keeps a new chat form and an in-flight start request", async () => {
  const app = mount();
  await app.$(".launch").click();
  const form = app.$("form.start");
  app.$("#cl-email").value = "pat@example.com";
  app.$("#cl-msg").value = "My order is damaged";
  const submit = form.requestSubmit();
  await app.$(".close").click();
  await app.$(".launch").click();
  assert.ok(app.$("form.start") === form);
  await form.requestSubmit();
  assert.equal(app.requests.length, 1);
  app.requests[0].respond(initial);
  await submit;
  assert.ok(app.$("form.compose"));
});

test("a stale poll cannot replace state after opening the live chat", async () => {
  const app = mount({ session: initial });
  const oldPoll = app.requests[0];
  await app.$(".launch").click();
  app.requests[1].respond({ state: "gathering", messages: initial.messages });
  await flush();
  const stream = app.streams.at(-1);
  await stream.dispatch("state", { data: JSON.stringify({ state: "resolved" }) });
  oldPoll.respond({ state: "new", messages: initial.messages });
  await flush();
  assert.match(app.$(".status").textContent, /^Resolved/);
});

test("an expired chat clears attachments and ignores queued events from its old stream", async () => {
  const app = await startChat();
  const oldStream = app.streams.at(-1);
  await pickPhoto(app);
  await oldStream.dispatch("state", { data: JSON.stringify({ state: "closed" }) });
  assert.ok(app.$("form.start"));
  assert.equal(app.stored.has("caseloop:pk_test"), false);
  assert.deepEqual(app.revoked, ["blob:photo.png"]);
  await oldStream.dispatch("state", { data: JSON.stringify({ state: "resolved" }) });
  assert.match(app.$(".status").textContent, /^Tell us/);
});

test("404 during restore resets the expired chat instead of offering a retry", async () => {
  const app = mount({ session: initial });
  await app.$(".launch").click();
  app.requests.at(-1).respond({ error: { message: "chat session not found" } }, 404);
  await flush();
  assert.equal(app.stored.has("caseloop:pk_test"), false);
  assert.ok(app.$("#cl-email"));
  assert.equal(app.$("form.retry"), null);
});

test("SSE fallback polls serially and deduplicates replies", async () => {
  const app = await startChat();
  const stream = app.streams.at(-1);
  stream.close();
  await stream.dispatch("error");
  assert.equal(app.intervals.size, 1);
  const count = app.requests.length;
  [...app.intervals.values()][0]();
  assert.equal(app.requests.length, count, "an unfinished poll should not overlap another");
  const reply = message("reply", "outbound");
  app.requests.at(-1).respond({ state: "awaiting_customer", messages: [reply, reply] });
  await flush();
  assert.equal(app.$(".body").children.filter((el) => el.classList.contains("msg")).length, 2);
});

test("returning to a cached page restores live updates", async () => {
  const app = await startChat();
  const stream = app.streams.at(-1);
  await app.window.dispatch("pagehide");
  assert.equal(stream.readyState, 2);
  await app.window.dispatch("pageshow", { persisted: true });
  assert.equal(app.streams.length, 2);
  assert.equal(app.streams.at(-1).readyState, 1);
});

test("Enter used to confirm an IME composition does not send the draft", async () => {
  const app = await startChat();
  app.$("textarea").value = "你好";
  await app.$("textarea").dispatch("keydown", { key: "Enter", shiftKey: false, isComposing: true });
  assert.equal(app.requests.length, 1);
  assert.equal(app.$("textarea").value, "你好");
});

test("an upload response only clears the photos included in that request", async () => {
  const app = await startChat();
  await pickPhoto(app, "first.png");
  const submit = app.$("form.compose").requestSubmit();
  // A file picker already open when Send was pressed can still finish while uploading.
  await pickPhoto(app, "second.png");
  app.requests.at(-1).respond({ state: "gathering", messages: [message("photo")] });
  await submit;
  assert.equal(app.$(".pending").children.length, 1);
  assert.deepEqual(app.revoked, ["blob:first.png"]);
});
