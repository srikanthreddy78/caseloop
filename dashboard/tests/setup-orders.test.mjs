import assert from "node:assert/strict";
import test from "node:test";
import { button, change, deferred, input, loadModule, mount, nodes, one, settle, textOf } from "./component-harness.mjs";

const sourceInfo = { configured: [], stored_counts: {}, forward_address: "orders@example.com" };
const business = {
  name: 'Acme "Lamps" & <More>', widget_public_key: "pk_example", context_text: "Policy",
  escalation_email: "owner@example.com", followup_schedule_hours: [24, 72], max_followups: 3,
  order_email_senders: [], inbound_email: "support@example.com", api_key_prefix: "ck_example",
};
const context = { business, member: null, setBusiness() {} };
const order = (number) => ({ order_number: number, source: "csv", source_label: "CSV import", status: "shipped", items: [] });
const lookup = { found: [order("LOOKUP")], sources_checked: ["CSV import"], errors: [], no_order_system_connected: false };
const OrderCard = () => null;
const SourceBadge = () => null;
const OrderSources = () => null;
const Lessons = () => null;
const Queue = () => null;
const Team = () => null;
const sourcesUrl = new URL("../src/components/OrderSources.tsx", import.meta.url);

function orders(t, api) {
  const component = mount(new URL("../src/pages/Orders.tsx", import.meta.url), "Orders", {
    api, context, imports: { "../components/OrderCard": { OrderCard, SourceBadge } },
  });
  t.after(() => component.cleanup());
  component.render();
  return component;
}

function sources(t, api) {
  const component = mount(sourcesUrl, "OrderSources", { api });
  t.after(() => component.cleanup());
  component.render();
  return component;
}

test("Orders recovers from an initial fetch failure", async (t) => {
  let fail = true;
  const component = orders(t, async (path) => {
    if (fail) throw new Error("Orders unavailable");
    return { data: path === "/v1/orders/sources" ? sourceInfo : [] };
  });
  await settle();
  assert.match(textOf(component.render()), /Orders unavailable/);
  fail = false;
  await button(component.render(), "Retry").props.onClick();
  assert.equal(button(component.render(), "Search").type, "button");
  assert.doesNotMatch(textOf(component.render()), /Orders unavailable/);
});

test("Orders lookup failure is visible and clears the previous result", async (t) => {
  let fail = false;
  const component = orders(t, async (path) => {
    if (path === "/v1/orders/sources/test") {
      if (fail) throw new Error("Lookup unavailable");
      return { data: lookup };
    }
    return { data: path === "/v1/orders/sources" ? sourceInfo : [] };
  });
  await settle();
  change(input(component.render(), "Order number"), "FIRST");
  await button(component.render(), "Look up").props.onClick();
  assert.equal(nodes(component.render(), (node) => node.type === OrderCard).length, 1);
  fail = true;
  change(input(component.render(), "Order number"), "SECOND");
  await button(component.render(), "Look up").props.onClick();
  assert.match(textOf(component.render()), /Lookup unavailable/);
  assert.equal(nodes(component.render(), (node) => node.type === OrderCard).length, 0);
  assert.equal(button(component.render(), "Look up").props.disabled, false);
});

test("A slower old search cannot overwrite the latest Orders search", async (t) => {
  const first = deferred();
  const second = deferred();
  const component = orders(t, async (path) => {
    if (path === "/v1/orders?q=FIRST") return first.promise;
    if (path === "/v1/orders?q=SECOND") return second.promise;
    return { data: path === "/v1/orders/sources" ? sourceInfo : [] };
  });
  await settle();
  change(input(component.render(), "Search by order number, email, or phone"), "FIRST");
  const pendingFirst = button(component.render(), "Search").props.onClick();
  change(input(component.render(), "Search by order number, email, or phone"), "SECOND");
  const pendingSecond = button(component.render(), "Search").props.onClick();
  second.resolve({ data: [order("SECOND")] });
  await pendingSecond;
  first.resolve({ data: [order("FIRST")] });
  await pendingFirst;
  assert.match(textOf(component.render()), /#SECOND/);
  assert.doesNotMatch(textOf(component.render()), /#FIRST/);
});

test("CSV import preserves the submitted search instead of showing all orders", async (t) => {
  const requested = [];
  const component = orders(t, async (path) => {
    requested.push(path);
    if (path === "/v1/orders/import") return { data: { imported: 1, source_label: "CSV import", columns_used: {} } };
    return { data: path === "/v1/orders/sources" ? sourceInfo : [] };
  });
  await settle();
  change(input(component.render(), "Search by order number, email, or phone"), "MATCH");
  await button(component.render(), "Search").props.onClick();
  // Typing without submitting must not change the filter during a refresh either.
  change(input(component.render(), "Search by order number, email, or phone"), "DRAFT");
  one(component.render(), (node) => node.type === "input" && node.props.type === "file").props.onChange({
    target: { files: [{ text: async () => "order_number\nMATCH" }], value: "orders.csv" },
  });
  await settle();
  assert.equal(requested.at(-1), "/v1/orders?q=MATCH");
  assert.match(textOf(component.render()), /Imported 1 orders/);
});

test("A lookup completing later preserves a newer submitted search", async (t) => {
  const result = deferred();
  const requested = [];
  const component = orders(t, async (path) => {
    requested.push(path);
    if (path === "/v1/orders/sources/test") return result.promise;
    return { data: path === "/v1/orders/sources" ? sourceInfo : [] };
  });
  await settle();
  change(input(component.render(), "Order number"), "LOOKUP");
  const pending = button(component.render(), "Look up").props.onClick();
  change(input(component.render(), "Search by order number, email, or phone"), "LATEST");
  await button(component.render(), "Search").props.onClick();
  result.resolve({ data: lookup });
  await pending;
  assert.equal(requested.at(-1), "/v1/orders?q=LATEST");
});

test("Source setup recovers from a failed initial fetch", async (t) => {
  let fail = true;
  const component = sources(t, async () => {
    if (fail) throw new Error("Sources unavailable");
    return { data: sourceInfo };
  });
  await settle();
  assert.match(textOf(component.render()), /Sources unavailable/);
  fail = false;
  await button(component.render(), "Retry order sources").props.onClick();
  assert.equal(button(component.render(), "Save sources").props.type, "submit");
});

test("Source form submits sources, keeps masked credentials, and blocks concurrent edits", async (t) => {
  const saved = deferred();
  const config = { id: "source-one", type: "shopify", shop: "acme.myshopify.com", token: "••••" };
  const writes = [];
  const component = sources(t, async (path, init) => {
    if (init?.method === "PUT") { writes.push({ path, body: JSON.parse(init.body) }); return saved.promise; }
    return { data: { ...sourceInfo, configured: [config] } };
  });
  await settle();
  let prevented = false;
  one(component.render(), (node) => node.type === "form").props.onSubmit({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, "/v1/orders/sources");
  assert.deepEqual(writes[0].body.order_sources, [config]);
  for (const { node } of nodes(component.render(), (node) => ["input", "textarea", "select", "button"].includes(node.type))) {
    assert.equal(node.props.disabled, true);
  }
  one(component.render(), (node) => node.type === "form").props.onSubmit({ preventDefault() {} });
  assert.equal(writes.length, 1);
  saved.resolve({ data: [config] });
  await settle();
  assert.equal(button(component.render(), "Save sources").props.disabled, false);
});

test("An older source request cannot overwrite edits after StrictMode effect replay", async (t) => {
  const first = deferred();
  const second = deferred();
  let calls = 0;
  const component = sources(t, () => ++calls === 1 ? first.promise : second.promise);
  component.replayEffects();
  const response = { data: { ...sourceInfo, configured: [{ type: "shopify", shop: "acme.myshopify.com" }] } };
  second.resolve(response);
  await settle();
  change(input(component.render(), "acme.myshopify.com"), "edited.myshopify.com");
  first.resolve(response);
  await settle();
  assert.equal(input(component.render(), "acme.myshopify.com").props.value, "edited.myshopify.com");
});

test("Source setup rejects JSON that would break lookups and accepts status maps", async (t) => {
  const cases = [
    { field: "headers", json: "1", error: /headers must be a JSON object/ },
    { field: "mapping", json: "[]", error: /mapping must be a JSON object/ },
    { field: "mapping", json: '{"status": 42}', error: /mapping values must be strings/ },
    { field: "mapping", json: '{"status_map": {"done": 42}}', error: /status_map must map status names to strings/ },
    { field: "mapping", json: '{"status": "state", "status_map": {"done": "delivered"}}' },
  ];
  for (const entry of cases) {
    const writes = [];
    const component = sources(t, async (_path, init) => {
      if (init?.method === "PUT") { writes.push(JSON.parse(init.body)); return { data: [] }; }
      return { data: { ...sourceInfo, configured: [{ type: "custom_api", url_template: "https://example.com/{order_number}" }] } };
    });
    await settle();
    const field = entry.field === "headers"
      ? input(component.render(), '{"Authorization": "Bearer …"}')
      : one(component.render(), (node) => node.type === "textarea");
    change(field, entry.json);
    one(component.render(), (node) => node.type === "form").props.onSubmit({ preventDefault() {} });
    await settle();
    if (entry.error) {
      assert.equal(writes.length, 0);
      assert.match(textOf(component.render()), entry.error);
    } else {
      assert.equal(writes.length, 1);
      assert.deepEqual(writes[0].order_sources[0].mapping, JSON.parse(entry.json));
    }
  }
});

test("Setup isolates source and team forms and escapes the copied widget name", (t) => {
  const component = mount(new URL("../src/pages/Settings.tsx", import.meta.url), "Settings", {
    context,
    imports: {
      "../components/OrderSources": { OrderSources }, "../components/Queue": { Queue }, "../components/Team": { Team },
      "../components/Lessons": { Lessons },
    },
  });
  t.after(() => component.cleanup());
  const tree = component.render();
  for (const { ancestors } of nodes(tree, (node) => node.type === OrderSources || node.type === Team)) {
    assert.equal(ancestors.some((ancestor) => ancestor.type === "form"), false);
  }
  assert.equal(one(tree, (node) => node.type === "textarea" && node.props.readOnly).props.value,
    '<script src="http://localhost:5173/widget.js" data-key="pk_example" data-name="Acme &quot;Lamps&quot; &amp; &lt;More&gt;"></script>');
});

test("The widget URL copied from Vite is proxied to the backend", () => {
  const config = loadModule(new URL("../vite.config.ts", import.meta.url), {
    vite: { defineConfig: (value) => value },
    "@vitejs/plugin-react": () => null,
    "@tailwindcss/vite": () => null,
  }).default;
  assert.equal(config.server.proxy["/widget.js"], config.server.proxy["/v1"]);
});

test("Queue retries are serialized so overlapping refreshes cannot restore failed jobs", async (t) => {
  const pending = deferred();
  const jobs = ["one", "two"].map((id) => ({ id, kind: "agent_turn", attempts: 3, updated_at: "2026-10-11T00:00:00Z" }));
  const retried = [];
  const component = mount(new URL("../src/components/Queue.tsx", import.meta.url), "Queue", {
    api: async (path, init) => {
      if (init?.method === "POST") { retried.push(path); return pending.promise; }
      return { data: retried.length ? [jobs[1]] : jobs };
    },
  });
  t.after(() => component.cleanup());
  component.render();
  await settle();
  const first = nodes(component.render(), (node) => node.type === "button")[0].node.props.onClick();
  const buttons = nodes(component.render(), (node) => node.type === "button");
  assert.equal(buttons.every(({ node }) => node.props.disabled), true);
  await buttons[1].node.props.onClick();
  assert.equal(retried.length, 1);
  pending.resolve({});
  await first;
  assert.equal(button(component.render(), "Retry").props.disabled, false);
});

test("A stale initial Queue request cannot restore a job already retried", async (t) => {
  const first = deferred();
  const job = { id: "one", kind: "agent_turn", attempts: 3 };
  let reads = 0;
  const component = mount(new URL("../src/components/Queue.tsx", import.meta.url), "Queue", {
    api: async (_path, init) => {
      if (init?.method === "POST") return {};
      if (++reads === 1) return first.promise;
      return { data: reads === 2 ? [job] : [] };
    },
  });
  t.after(() => component.cleanup());
  component.render();
  component.replayEffects();
  await settle();
  await button(component.render(), "Retry").props.onClick();
  first.resolve({ data: [job] });
  await settle();
  assert.match(textOf(component.render()), /Nothing has failed/);
});
