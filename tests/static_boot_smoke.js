'use strict';
/* Boot smoke test for the built GitHub Pages site (docs/).
   Runs the real built index.html inline script with DOM/localStorage/fetch
   shims plus the real engine.js, then drives the client-side static API:
   - boot must detect the static health marker and activate applyStaticMode()
   - POST /api/process/simulate must solve a recycle circuit in-browser
   Run after `python3 build_pages.py`. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const DOCS = path.join(__dirname, '..', 'docs');

/* ---- minimal browser shims ---- */
function makeStub() {
  const store = {};
  const fn = function () { return stub; };
  const stub = new Proxy(fn, {
    get(_t, p) {
      if (p === Symbol.toPrimitive) return () => '';
      if (p === 'then') return undefined; // never a thenable
      if (p in store) return store[p];
      store[p] = makeStub();
      return store[p];
    },
    set(_t, p, v) { store[p] = v; return true; },
    apply() { return stub; },
  });
  return stub;
}
const storage = new Map();
global.localStorage = {
  getItem: k => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)),
  removeItem: k => storage.delete(k),
};
global.document = {
  getElementById: () => makeStub(),
  querySelectorAll: () => [],
  createElement: () => makeStub(),
  addEventListener: () => {},
  removeEventListener: () => {},
};
global.Chart = class { destroy() {} };
global.prompt = () => null;

/* fetch serves the real built static files from docs/ */
global.fetch = async url => {
  const clean = String(url).split('?')[0].replace(/^\/+/, '');
  const file = path.join(DOCS, clean);
  if (!fs.existsSync(file)) return { ok: false, status: 404 };
  return { ok: true, status: 200, json: async () => JSON.parse(fs.readFileSync(file, 'utf8')) };
};

global.window = global;

/* load the engine exactly as the browser would */
const engine = require('../app/static_assets/engine.js');
assert.ok(engine.SXEWProcessEngine, 'engine.js must export SXEWProcessEngine');

/* run the real built inline script */
const html = fs.readFileSync(path.join(DOCS, 'index.html'), 'utf8');
const inline = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(inline, 'built index.html must contain an inline script');
vm.runInThisContext(inline[1], { filename: 'docs/index.html#inline' });

(async () => {
  /* let the async boot IIFE settle */
  for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r));

  assert.strictEqual(localStorage.getItem('sx_token'), 'static',
    'boot must activate static mode from the health marker');
  assert.strictEqual(typeof global.api, 'function', 'static api() override must be installed');

  const circuit = {
    feed: { flow: 1000, Cu: 0.02, Fe: 0.01, H2SO4: 0.05 },
    nodes: [
      { id: 'F', type: 'Feed', params: {} },
      { id: 'E1', type: 'Ex', params: { efficiency: 90, organic_flow_ratio: 1 } },
      { id: 'E2', type: 'Ex', params: { efficiency: 92, organic_flow_ratio: 1 } },
      { id: 'P', type: 'Px', params: { efficiency: 95 } },
      { id: 'EW', type: 'EW', params: { cu_deposition: 98 } },
    ],
    edges: [
      { source: 'F', target: 'E1', port: 'out' },
      { source: 'E1', target: 'E2', port: 'raffinate' },
      { source: 'E2', target: 'P', port: 'organic' },
      { source: 'P', target: 'EW', port: 'electrolyte' },
      { source: 'P', target: 'E2', port: 'lean_organic', recycle: true },
    ],
    solver: { max_iter: 500, tolerance: 1e-9, relaxation: 0.65 },
  };
  const r = await global.api('/api/process/simulate', {
    method: 'POST',
    body: JSON.stringify({ circuit }),
  });
  /* Reference value computed by app/process_engine.py (test_process.py circuit):
     the JS port must match the Python engine to solver precision. */
  const PY_CU_RECOVERY = 8.127288225976988;
  assert.ok(r.kpis && Math.abs(r.kpis.Cu_recovery_pct - PY_CU_RECOVERY) < 1e-9,
    `JS engine must match Python reference recovery ${PY_CU_RECOVERY} (got ${r.kpis && r.kpis.Cu_recovery_pct})`);
  assert.strictEqual(r.solver.converged, true);
  assert.ok(Math.abs(r.kpis.mass_balance.error.Cu) < 1e-7);

  const health = await global.api('api/health');
  assert.strictEqual(health.mode, 'static-client-side');

  console.log('ok - static boot activated from health marker');
  console.log(`ok - client-side simulate: Cu recovery ${r.kpis.Cu_recovery_pct.toFixed(2)}%, converged in ${r.solver.iterations} iterations`);
  console.log('static boot smoke test passed');
})().catch(e => { console.error(e); process.exit(1); });
