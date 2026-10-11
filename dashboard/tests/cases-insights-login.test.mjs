import assert from "node:assert/strict";
import test from "node:test";
import { button, change, deferred, mount, one, settle, textOf } from "./component-harness.mjs";

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

const Link = () => null;
const StatePill = () => null;
const OrderCard = () => null;
const Attachments = () => null;
const orders = { found: [], sources_checked: [], errors: [], no_order_system_connected: true };
const caseData = (id, state = "awaiting_customer") => ({
  id, state, subject: `Case ${id}`, channel: "email", customer: { name: `Customer ${id}`, email: `${id}@example.com` },
  requirements: [], followups_sent: 0, messages: [], updated_at: "2026-10-11T00:00:00Z",
});
const snapshot = (headline) => ({
  case_count: 10, generated_at: "2026-10-11T00:00:00Z", model: "rules",
  report: { headline, themes: [], sentiment_trend: "Mixed", emerging_issues: [], top_escalation_reasons: [] },
});

function page(t, name, api, { id = "one", search = "", setApiKey = () => {} } = {}) {
  const route = { id, params: new URLSearchParams(search), navigations: [] };
  const navigate = (...args) => route.navigations.push(args);
  const component = mount(new URL(`../src/pages/${name}.tsx`, import.meta.url), name, {
    api,
    imports: {
      "react-router-dom": {
        Link, useNavigate: () => navigate, useParams: () => ({ id: route.id }),
        useSearchParams: () => [route.params, (next) => { route.params = new URLSearchParams(typeof next === "function" ? next(route.params) : next); }],
      },
      "../api": { api, ApiError, ago: () => "just now", fmtTime: () => "today", setApiKey },
      "../components/StatePill": { StatePill }, "../components/OrderCard": { OrderCard }, "../components/Attachments": { Attachments },
    },
  });
  t.after(() => component.cleanup());
  component.render();
  return { ...component, route };
}

function queue() {
  const requests = [];
  const api = (path, init) => {
    const pending = deferred();
    requests.push({ path, init, ...pending });
    return pending.promise;
  };
  return { api, requests };
}

function respondToCase(requests, id, { text = `Message ${id}`, failedOrders = false } = {}) {
  for (const request of requests.filter((r) => r.path.startsWith(`/v1/cases/${id}`) && !r.init?.method)) {
    if (request.path.endsWith("/timeline")) request.resolve({ data: [{ kind: "message", direction: "inbound", channel: "email", text }] });
    else if (request.path.endsWith("/orders")) {
      if (failedOrders) request.reject(new Error("Order service unavailable"));
      else request.resolve({ data: orders });
    } else request.resolve({ data: caseData(id) });
  }
}

test("Cases shows a failed request and retries it", async (t) => {
  let fail = true;
  const component = page(t, "Cases", async () => {
    if (fail) throw new Error("Cases unavailable");
    return { data: [caseData("one")], meta: { total: 1 } };
  });
  await settle();
  assert.match(textOf(component.render()), /Cases unavailable/);
  fail = false;
  button(component.render(), "Retry").props.onClick();
  component.render();
  await settle();
  assert.match(textOf(component.render()), /Case one/);
  assert.doesNotMatch(textOf(component.render()), /Cases unavailable/);
});

test("Cases keeps the selected filter when an older request completes later", async (t) => {
  const { api, requests } = queue();
  const component = page(t, "Cases", api);
  button(component.render(), "Resolved").props.onClick();
  component.render();
  requests[1].resolve({ data: [caseData("resolved", "resolved")], meta: { total: 1 } });
  await settle();
  requests[0].resolve({ data: [caseData("old-open")], meta: { total: 1 } });
  await settle();
  assert.match(textOf(component.render()), /Case resolved/);
  assert.doesNotMatch(textOf(component.render()), /old-open/);
});

test("Cases lets slow requests complete and retains rows while polling", async (t) => {
  const { api, requests } = queue();
  const component = page(t, "Cases", api);
  await component.advanceTimersByTime(15000);
  component.render();
  assert.equal(requests.length, 1);
  requests[0].resolve({ data: [caseData("first")], meta: { total: 1 } });
  await settle();
  assert.match(textOf(component.render()), /Case first/);
  await component.advanceTimersByTime(4999);
  assert.equal(requests.length, 1);
  await component.advanceTimersByTime(1);
  assert.equal(requests.length, 2);
  assert.match(textOf(component.render()), /Case first/);
  await component.advanceTimersByTime(15000);
  component.render();
  assert.equal(requests.length, 2);
  requests[1].resolve({ data: [caseData("updated")], meta: { total: 1 } });
  await settle();
  assert.match(textOf(component.render()), /Case updated/);
});

test("An old Cases poll cannot overwrite a newly selected filter", async (t) => {
  const { api, requests } = queue();
  const component = page(t, "Cases", api);
  requests[0].resolve({ data: [caseData("open")], meta: { total: 1 } });
  await settle();
  await component.advanceTimersByTime(5000);
  button(component.render(), "Resolved").props.onClick();
  component.render();
  assert.doesNotMatch(textOf(component.render()), /Case open/);
  requests[2].resolve({ data: [caseData("resolved", "resolved")], meta: { total: 1 } });
  await settle();
  requests[1].resolve({ data: [caseData("old-poll")], meta: { total: 1 } });
  await settle();
  assert.match(textOf(component.render()), /Case resolved/);
  assert.doesNotMatch(textOf(component.render()), /old-poll/);
});

test("Cases polling recovers from a transient error", async (t) => {
  let requests = 0;
  const component = page(t, "Cases", async () => {
    requests++;
    if (requests === 2) throw new Error("Temporary outage");
    return { data: [caseData(requests === 1 ? "first" : "recovered")], meta: { total: 1 } };
  });
  await settle();
  await component.advanceTimersByTime(5000);
  assert.match(textOf(component.render()), /Temporary outage/);
  await component.advanceTimersByTime(5000);
  assert.match(textOf(component.render()), /Case recovered/);
  assert.doesNotMatch(textOf(component.render()), /Temporary outage/);
  assert.equal(requests, 3);
});

test("Cases stops polling when unmounted, including after an in-flight load", async (t) => {
  for (const finishBeforeCleanup of [true, false]) {
    const { api, requests } = queue();
    const component = page(t, "Cases", api);
    if (finishBeforeCleanup) {
      requests[0].resolve({ data: [caseData("one")], meta: { total: 1 } });
      await settle();
    }
    component.cleanup();
    if (!finishBeforeCleanup) {
      requests[0].resolve({ data: [caseData("one")], meta: { total: 1 } });
      await settle();
    }
    await component.advanceTimersByTime(15000);
    assert.equal(requests.length, 1);
  }
});

test("Cases reaches records after the first hundred and resets the page on filter change", async (t) => {
  const paths = [];
  const component = page(t, "Cases", async (path) => {
    paths.push(path);
    const offset = Number(new URL(path, "http://localhost").searchParams.get("offset"));
    return { data: Array.from({ length: offset ? 1 : 100 }, (_, i) => caseData(String(offset + i + 1))), meta: { total: 101 } };
  });
  await settle();
  assert.equal(button(component.render(), "Previous").props.disabled, true);
  button(component.render(), "Next").props.onClick();
  component.render();
  await settle();
  assert.match(textOf(component.render()), /Case 101/);
  assert.equal(button(component.render(), "Next").props.disabled, true);
  assert.equal(component.route.params.get("offset"), "100");
  button(component.render(), "Resolved").props.onClick();
  component.render();
  await settle();
  assert.equal(component.route.params.has("offset"), false);
  const query = new URL(paths.at(-1), "http://localhost").searchParams;
  assert.equal(query.get("offset"), "0");
  assert.equal(query.get("state"), "resolved");
});

test("Case details recover from an initial load failure", async (t) => {
  let fail = true;
  const component = page(t, "CaseDetail", async (path) => {
    if (path.endsWith("/orders")) return { data: orders };
    if (path.endsWith("/timeline")) return { data: [] };
    if (fail) throw new Error("Case unavailable");
    return { data: caseData("one") };
  });
  await settle();
  assert.match(textOf(component.render()), /Case unavailable/);
  fail = false;
  button(component.render(), "Retry").props.onClick();
  await settle();
  assert.match(textOf(component.render()), /Case one/);
});

test("Case details report failed order lookup instead of looking forever", async (t) => {
  const { api, requests } = queue();
  const component = page(t, "CaseDetail", api);
  respondToCase(requests, "one", { failedOrders: true });
  await settle();
  assert.match(textOf(component.render()), /Could not load orders: Order service unavailable/);
  assert.doesNotMatch(textOf(component.render()), /Looking…/);
});

test("Late case responses cannot replace the case opened next", async (t) => {
  const { api, requests } = queue();
  const component = page(t, "CaseDetail", api);
  component.route.id = "two";
  component.render();
  respondToCase(requests, "two");
  await settle();
  respondToCase(requests, "one");
  await settle();
  const tree = component.render();
  assert.match(textOf(tree), /Case two/);
  assert.match(textOf(tree), /Message two/);
  assert.doesNotMatch(textOf(tree), /Case one|Message one/);
});

test("A delivered reply clears once even when refreshing the case fails", async (t) => {
  const sent = deferred();
  let postCount = 0;
  const component = page(t, "CaseDetail", async (path, init) => {
    if (init?.method === "POST") { postCount++; return sent.promise; }
    if (postCount) throw new Error("Refresh unavailable");
    if (path.endsWith("/orders")) return { data: orders };
    if (path.endsWith("/timeline")) return { data: [] };
    return { data: caseData("one") };
  });
  await settle();
  change(one(component.render(), (n) => n.type === "textarea"), "Thanks for writing");
  const submit = one(component.render(), (n) => n.type === "form").props.onSubmit;
  const first = submit({ preventDefault() {} });
  const duplicate = submit({ preventDefault() {} });
  assert.equal(postCount, 1);
  assert.equal(one(component.render(), (n) => n.type === "textarea").props.disabled, true);
  sent.resolve({ data: caseData("one") });
  await Promise.all([first, duplicate]);
  const tree = component.render();
  assert.equal(one(tree, (n) => n.type === "textarea").props.value, "");
  assert.match(textOf(tree), /Could not refresh the case: Refresh unavailable/);
  assert.equal(button(tree, "Send reply").props.disabled, true);
});

test("A failed reply preserves its draft and refreshes saved delivery failure", async (t) => {
  let failed = false;
  const component = page(t, "CaseDetail", async (path, init) => {
    if (init?.method === "POST") { failed = true; throw new ApiError(502, "Message saved but not delivered"); }
    if (path.endsWith("/orders")) return { data: orders };
    if (path.endsWith("/timeline")) return { data: failed ? [{ kind: "message", id: "message-one", sender: "human", direction: "outbound", text: "Saved reply", meta: { delivery: "failed" } }] : [] };
    return { data: caseData("one") };
  });
  await settle();
  change(one(component.render(), (n) => n.type === "textarea"), "Saved reply");
  await one(component.render(), (n) => n.type === "form").props.onSubmit({ preventDefault() {} });
  const tree = component.render();
  assert.equal(one(tree, (n) => n.type === "textarea").props.value, "Saved reply");
  assert.match(textOf(tree), /Message saved but not delivered/);
  assert.equal(button(tree, "Resend").props.disabled, false);
});

test("A reply completing after navigation does not clear the next case's draft", async (t) => {
  const sent = deferred();
  const paths = [];
  const component = page(t, "CaseDetail", async (path, init) => {
    paths.push(path);
    if (init?.method === "POST") return sent.promise;
    if (path.endsWith("/orders")) return { data: orders };
    if (path.endsWith("/timeline")) return { data: [] };
    return { data: caseData(path.split("/").at(-1)) };
  });
  await settle();
  change(one(component.render(), (n) => n.type === "textarea"), "Reply for one");
  const submitted = one(component.render(), (n) => n.type === "form").props.onSubmit({ preventDefault() {} });
  component.route.id = "two";
  component.render();
  await settle();
  change(one(component.render(), (n) => n.type === "textarea"), "Draft for two");
  const requestsBeforeCompletion = paths.length;
  sent.resolve({ data: caseData("one") });
  await submitted;
  const tree = component.render();
  assert.equal(one(tree, (n) => n.type === "textarea").props.value, "Draft for two");
  assert.equal(one(tree, (n) => n.type === "textarea").props.disabled, false);
  assert.match(textOf(tree), /Case two/);
  assert.equal(paths.length, requestsBeforeCompletion);
});

test("Insights shows a load failure and can recover to the no-report state", async (t) => {
  let fail = true;
  const component = page(t, "Insights", async (path) => {
    if (path.startsWith("/v1/cases")) return { data: [], meta: { total: 0 } };
    throw new ApiError(fail ? 503 : 404, fail ? "Report unavailable" : "No report");
  });
  await settle();
  assert.match(textOf(component.render()), /Report unavailable/);
  fail = false;
  button(component.render(), "Retry").props.onClick();
  await settle();
  assert.match(textOf(component.render()), /No report yet/);
  assert.doesNotMatch(textOf(component.render()), /Report unavailable/);
});

test("Failed report generation stays visible, preserves the report, and can be retried", async (t) => {
  let fail = true;
  const component = page(t, "Insights", async (path) => {
    if (path.startsWith("/v1/cases")) return { data: [], meta: { total: 0 } };
    if (path.endsWith("/latest")) return { data: snapshot("Original report") };
    if (fail) throw new Error("Generation unavailable");
    return { data: snapshot("Updated report") };
  });
  await settle();
  await button(component.render(), "Refresh now").props.onClick();
  assert.match(textOf(component.render()), /Generation unavailable/);
  assert.match(textOf(component.render()), /Original report/);
  assert.equal(button(component.render(), "Refresh now").props.disabled, false);
  fail = false;
  await button(component.render(), "Refresh now").props.onClick();
  assert.match(textOf(component.render()), /Updated report/);
  assert.doesNotMatch(textOf(component.render()), /Generation unavailable/);
});

test("Login accepts a new token after a missing or invalid link", async (t) => {
  const requests = [];
  const component = page(t, "Login", async (_path, init) => { requests.push(JSON.parse(init.body).token); return {}; });
  assert.match(textOf(component.render()), /missing its token/);
  component.route.params = new URLSearchParams({ token: "valid-token" });
  component.render();
  await settle();
  assert.deepEqual(requests, ["valid-token"]);
  assert.equal(component.route.navigations.length, 1);
  assert.equal(component.route.navigations[0][0], "/cases");
});

test("Insights counts all case pages instead of stopping at two hundred", async (t) => {
  const offsets = [];
  const component = page(t, "Insights", async (path) => {
    if (path.startsWith("/v1/cases")) {
      const offset = Number(new URL(path, "http://localhost").searchParams.get("offset"));
      offsets.push(offset);
      return {
        data: Array.from({ length: offset ? 5 : 200 }, (_, i) => caseData(String(offset + i), offset ? "resolved" : "awaiting_customer")),
        meta: { total: 205 },
      };
    }
    return { data: snapshot("All case counts") };
  });
  await settle();
  assert.deepEqual(offsets, [0, 200]);
  assert.match(textOf(component.render()), /205Email/);
  assert.match(textOf(component.render()), /200Waiting on customer/);
  assert.match(textOf(component.render()), /5Resolved/);
});

test("Refreshing Insights also refreshes the case counts", async (t) => {
  let generated = false;
  let countRequests = 0;
  const component = page(t, "Insights", async (path) => {
    if (path.startsWith("/v1/cases")) {
      countRequests++;
      return { data: generated ? [caseData("one"), caseData("two")] : [caseData("one")], meta: { total: generated ? 2 : 1 } };
    }
    if (path.endsWith("/generate")) generated = true;
    return { data: snapshot("Case counts") };
  });
  await settle();
  assert.match(textOf(component.render()), /1Email/);
  await button(component.render(), "Refresh now").props.onClick();
  assert.equal(countRequests, 2);
  assert.match(textOf(component.render()), /2Email/);
});

test("Failed Insights count loads show unavailable values and can be retried", async (t) => {
  let fail = true;
  const component = page(t, "Insights", async (path) => {
    if (path.startsWith("/v1/cases")) {
      if (fail) throw new Error("Counts unavailable");
      return { data: [caseData("one")], meta: { total: 1 } };
    }
    return { data: snapshot("Case counts") };
  });
  await settle();
  assert.match(textOf(component.render()), /Could not load case counts: Counts unavailable/);
  assert.match(textOf(component.render()), /—Email/);
  assert.doesNotMatch(textOf(component.render()), /0Email/);
  fail = false;
  button(component.render(), "Retry counts").props.onClick();
  await settle();
  assert.match(textOf(component.render()), /1Email/);
  assert.doesNotMatch(textOf(component.render()), /Counts unavailable/);
});

test("Login reuses the single-use token request during StrictMode effect replay", async (t) => {
  const pending = deferred();
  let calls = 0;
  const keys = [];
  const component = page(t, "Login", () => { calls++; return pending.promise; }, { search: "token=once", setApiKey: (key) => keys.push(key) });
  component.replayEffects();
  assert.equal(calls, 1);
  pending.resolve({});
  await settle();
  assert.deepEqual(keys, [null]);
  assert.equal(component.route.navigations.length, 1);
});

test("Login ignores a previous token's late error", async (t) => {
  const { api, requests } = queue();
  const component = page(t, "Login", api, { search: "token=old" });
  component.route.params = new URLSearchParams({ token: "new" });
  component.render();
  requests[0].reject(new Error("Old link expired"));
  await settle();
  assert.doesNotMatch(textOf(component.render()), /Old link expired/);
  requests[1].resolve({});
  await settle();
  assert.equal(component.route.navigations.length, 1);
});
