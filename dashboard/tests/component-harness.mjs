import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";
import ts from "typescript";

// Exercise the real component handlers without installing a DOM or a test renderer.
// render() commits state updates; async effects are drained with settle().
export function loadModule(url, imports, globals = {}) {
  const source = readFileSync(url, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2023 },
  }).outputText;
  const exports = {};
  const require = createRequire(url);
  vm.runInNewContext(output, {
    exports,
    require: (name) => Object.hasOwn(imports, name) ? imports[name] : require(name),
    ...globals,
  }, { filename: url.pathname });
  return exports;
}

export function mount(url, name, { api, context, imports = {}, props = {} } = {}) {
  const slots = [];
  const timers = new Set();
  let cursor = 0;
  let effects = [];
  const slot = (create) => {
    const index = cursor++;
    if (!(index in slots)) slots[index] = create();
    return slots[index];
  };
  const jsx = (type, props, key) => ({ type, props: props || {}, key });
  const changed = (previous, next) => !previous || !next || previous.length !== next.length || next.some((dep, index) => !Object.is(dep, previous[index]));
  const hooks = {
    useState(initial) {
      const state = slot(() => ({ value: typeof initial === "function" ? initial() : initial }));
      return [state.value, (value) => { state.value = typeof value === "function" ? value(state.value) : value; }];
    },
    useRef(initial) { return slot(() => ({ current: initial })); },
    useCallback(fn, deps) {
      const state = slot(() => ({}));
      if (changed(state.deps, deps)) { state.value = fn; state.deps = deps; }
      return state.value;
    },
    useEffect(effect, deps) {
      const state = slot(() => ({}));
      if (changed(state.deps, deps)) {
        effects.push(() => { state.cleanup?.(); state.cleanup = effect(); });
        state.effect = effect;
        state.deps = deps;
      }
    },
  };
  const module = loadModule(url, {
    react: hooks,
    "react/jsx-runtime": { jsx, jsxs: jsx, Fragment: "fragment" },
    "react-router-dom": { useOutletContext: () => context },
    "../api": { api, ago: () => "just now" },
    ...imports,
  }, {
    window: { location: { origin: "http://localhost:5173" } },
    URLSearchParams,
    URL,
    setTimeout(fn, delay) {
      const timer = setTimeout(fn, delay);
      timer.unref();
      timers.add(timer);
      return timer;
    },
    clearTimeout,
  });
  return {
    render() {
      cursor = 0;
      const tree = module[name](props);
      const pending = effects;
      effects = [];
      for (const commit of pending) commit();
      return tree;
    },
    replayEffects() {
      const active = slots.filter((state) => state.effect);
      for (const state of active) state.cleanup?.();
      for (const state of active) state.cleanup = state.effect();
    },
    cleanup() {
      for (const state of slots) state.cleanup?.();
      for (const timer of timers) clearTimeout(timer);
    },
  };
}

export function nodes(tree, predicate, ancestors = []) {
  if (Array.isArray(tree)) return tree.flatMap((child) => nodes(child, predicate, ancestors));
  if (!tree || typeof tree !== "object") return [];
  const found = predicate(tree) ? [{ node: tree, ancestors }] : [];
  return [...found, ...nodes(tree.props?.children, predicate, [...ancestors, tree])];
}

export function textOf(tree) {
  if (Array.isArray(tree)) return tree.map(textOf).join("");
  if (tree == null || typeof tree === "boolean") return "";
  if (typeof tree !== "object") return String(tree);
  return textOf(tree.props?.children);
}

export function one(tree, predicate) {
  const matches = nodes(tree, predicate);
  if (matches.length !== 1) throw new Error(`Expected one matching element, got ${matches.length}`);
  return matches[0].node;
}

export const button = (tree, label) => one(tree, (node) => node.type === "button" && textOf(node) === label);
export const input = (tree, placeholder) => one(tree, (node) => node.type === "input" && node.props.placeholder === placeholder);
export const change = (node, value) => node.props.onChange({ target: { value } });
export const settle = () => new Promise((resolve) => setImmediate(resolve));
export const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
