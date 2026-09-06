'use strict';
/* Node smoke test for the browser-side engine port (app/static_assets/engine.js).
   Mirrors test_process.py so the GitHub Actions build job verifies JS/Python
   parity on the same circuit before the static site is deployed. */
const assert = require('assert');
const { SXEWProcessEngine, optimize } = require('../app/static_assets/engine.js');

function circuit() {
  return {
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
}

function testRecycleConvergesAndCuBalance() {
  const r = new SXEWProcessEngine(circuit()).run();
  assert.strictEqual(r.solver.converged, true);
  assert.ok(r.solver.iterations > 1);
  assert.ok(Math.abs(r.kpis.mass_balance.error.Cu) < 1e-7, `Cu balance error ${r.kpis.mass_balance.error.Cu}`);
}

function testSankeyContainsRecycleNetwork() {
  const r = new SXEWProcessEngine(circuit()).run();
  assert.ok(r.sankey.some(x => x.source === 'P' && x.target === 'E2'));
}

function testParallelRequiresFractions() {
  const c = circuit();
  c.edges.push({ source: 'F', target: 'E2' });
  assert.throws(() => new SXEWProcessEngine(c).run(), /fraction/i);
}

function testMultiobjectiveParetoEngine() {
  const c = circuit();
  c.nodes[1].params.D_Cu = 2.5;
  c.nodes[1].params.O_A = 1.0;
  c.nodes[1].params.organic_inventory = 10;
  c.nodes[1].params.capacity_flow = 1500;
  const vars = [
    { path: 'nodes.E1.params.D_Cu', min: 1.0, max: 5.0, step: 0.2 },
    { path: 'nodes.E1.params.O_A', min: 0.5, max: 2.0, step: 0.1 },
  ];
  const obs = [{ objective: 'max_recovery' }, { objective: 'min_cost' }, { objective: 'max_capacity' }];
  const r = optimize(c, vars, obs, 30, 1e-8, 8, 7);
  assert.ok(r.pareto_count >= 1);
  assert.ok(r.algorithm.startsWith('NSGA-II'));
}

const tests = [
  testRecycleConvergesAndCuBalance,
  testSankeyContainsRecycleNetwork,
  testParallelRequiresFractions,
  testMultiobjectiveParetoEngine,
];

for (const t of tests) {
  t();
  console.log(`ok - ${t.name}`);
}
console.log(`${tests.length} engine.js smoke tests passed`);
