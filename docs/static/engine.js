/* Browser-side port of app/process_engine.py and app/optimizer.py.
   Kept 1:1 with the Python engine (same defaults, same validation, same
   KPI/Sankey/profile payload) and verified against it by tests/parity_check.py. */
'use strict';
const SPECIES = ['Cu', 'Fe', 'H2SO4'];
const EPS = 1e-12;

class ProcessError extends Error {}
class OptimizationError extends Error {}

const pct = v => { v = Number(v); return v > 1 ? v / 100.0 : Math.max(0, Math.min(1, v)); };
const clamp = (v, a = 0.0, b = 1.0) => Math.max(a, Math.min(b, Number(v)));
const eKey = e => `${e.source}|${e.target}|${e.port}|${e.recycle ? 'R' : 'N'}`;
// Lightweight stream record with a copy() helper matching the Python dataclass.
function stream(o) {
  return Object.assign({
    id: '', flow: 0.0, Cu: 0.0, Fe: 0.0, H2SO4: 0.0,
    phase: 'aqueous', source: null, target: null, port: 'out',
  }, o, {
    copy(kw) { return stream(Object.assign({}, this, kw)); },
    masses() { const m = {}; for (const x of SPECIES) m[x] = this.flow * this[x]; return m; },
  });
}

class SXEWProcessEngine {
  constructor(circuit) {
    this.nodes = {};
    for (const n of (circuit.nodes || [])) {
      const params = {};
      for (const [k, v] of Object.entries(n.params || {})) { const f = Number(v); if (Number.isFinite(f)) params[k] = f; }
      this.nodes[n.id] = { id: n.id, name: n.name || n.id, type: n.type || 'Stage', params };
    }
    this.edges = [];
    for (const raw of (circuit.edges || [])) {
      const e = Array.isArray(raw)
        ? { source: raw[0], target: raw[1], mode: raw.length > 2 ? raw[2] : 'series', fraction: raw.length > 3 ? raw[3] : undefined, port: raw.length > 4 ? raw[4] : 'out' }
        : raw;
      this.edges.push({ source: e.source, target: e.target, mode: e.mode || 'series', fraction: e.fraction, port: e.port || 'out', recycle: !!e.recycle });
    }
    const f = circuit.feed || {};
    this.feed = stream({ id: 'FEED', flow: Number(f.flow || 1000), Cu: Number(f.Cu || .02), Fe: Number(f.Fe || .01), H2SO4: Number(f.H2SO4 || .05), phase: f.phase || 'aqueous' });
    const solver = circuit.solver || {};
    this.max_iter = Math.max(1, parseInt(solver.max_iter || 500, 10));
    this.tol = Math.max(1e-12, Number(solver.tolerance || 1e-8));
    this.relax = clamp(solver.relaxation || .55, .05, .99);
    this.trace = [];
  }

  run() {
    this._validate();
    const inc = {}, out = {};
    for (const e of this.edges) {
      (inc[e.target] = inc[e.target] || []).push(e);
      (out[e.source] = out[e.source] || []).push(e);
    }
    const recycle = this.edges.filter(e => e.recycle);
    const order = this._topologicalOrder(this.edges.filter(e => !e.recycle));
    const guesses = new Map(recycle.map(e => [eKey(e), SXEWProcessEngine._zero(e)]));
    let residual = Infinity, producedBy = {}, edgeStreams = [], iteration = 0;
    for (iteration = 1; iteration <= this.max_iter; iteration++) {
      producedBy = {}; edgeStreams = [];
      for (const nid of order) {
        let incoming = edgeStreams.filter(([e]) => e.target === nid && !e.recycle).map(([, s]) => s);
        incoming = incoming.concat((inc[nid] || []).filter(e => e.recycle).map(e => guesses.get(eKey(e)).copy({ target: nid })));
        if (!incoming.length && (!(inc[nid] || []).length || !inc[nid].some(e => !e.recycle))) {
          incoming = [this.feed.copy({ id: `FEED->${nid}`, target: nid })];
        }
        const produced = this._transform(this.nodes[nid], incoming);
        producedBy[nid] = produced;
        for (const [port, base] of Object.entries(produced)) {
          const es = (out[nid] || []).filter(e => e.port === port || (e.port === 'out' && port === 'out'));
          if (!es.length) continue;
          const ws = this._weights(es);
          es.forEach((e, i) => {
            const s = base.copy({ id: `${nid}:${port}->${e.target}`, flow: base.flow * ws[i], source: nid, target: e.target, port });
            edgeStreams.push([e, s]);
          });
        }
      }
      residual = 0.0;
      for (const e of recycle) {
        const nue = edgeStreams.find(([ee]) => ee === e);
        const news = nue ? nue[1] : this._zero(e);
        const olds = guesses.get(eKey(e));
        residual = Math.max(residual, SXEWProcessEngine._delta(olds, news));
        guesses.set(eKey(e), this._relax(olds, news));
      }
      if (!recycle.length || (residual <= this.tol * 0.01 && iteration >= 2)) break;
    }
    if (recycle.length && residual > this.tol * 0.01) {
      throw new ProcessError(`Recycle solver did not converge after ${this.max_iter} iterations (residual=${residual.toExponential(3)})`);
    }

    const terminals = [];
    for (const [nid, produced] of Object.entries(producedBy)) {
      for (const [port, s] of Object.entries(produced)) {
        const es = (out[nid] || []).filter(e => e.port === port || (e.port === 'out' && port === 'out'));
        if (!es.length) terminals.push(s.copy({ id: `${nid}:${port}->OUT`, source: nid, target: 'OUT', port }));
      }
    }
    const streams = edgeStreams.filter(([e]) => !e.recycle).map(([, s]) => s);
    for (const e of recycle) { const g = guesses.get(eKey(e)); if (g.flow > EPS) streams.push(g); }
    streams.push(...terminals);

    const nodePayload = {};
    for (const [nid, prod] of Object.entries(producedBy)) {
      const inp = this._mix(this._inputsForNode(nid, inc, edgeStreams, guesses), nid);
      nodePayload[nid] = { name: this.nodes[nid].name, type: this.nodes[nid].type, input: plain(inp), outputs: mapValues(prod, plain), parameters: this.nodes[nid].params };
    }
    const profiles = [], isotherms = [];
    for (const [nid, payload] of Object.entries(nodePayload)) {
      const typ = String(payload.type).toLowerCase();
      if (['ex', 'extraction', 'extractor', 'px', 'stripper', 'stripping'].includes(typ)) {
        const inp = payload.input || {}, outs = payload.outputs || {};
        profiles.push({
          stage: profiles.length + 1, node_id: nid, name: payload.name,
          type: (typ.startsWith('ex') || typ === 'extractor') ? 'Extraction' : 'Stripper',
          feed_flow: inp.flow || 0, Cu_in: inp.Cu || 0, Fe_in: inp.Fe || 0, acid_in: inp.H2SO4 || 0,
          Cu_out: sum(Object.values(outs), v => (v.flow || 0) * (v.Cu || 0)),
          Fe_out: sum(Object.values(outs), v => (v.flow || 0) * (v.Fe || 0)),
          acid_out: sum(Object.values(outs), v => (v.flow || 0) * (v.H2SO4 || 0)),
          D_Cu: payload.parameters.D_Cu !== undefined ? payload.parameters.D_Cu : (payload.parameters.D || 0),
          O_A: payload.parameters.O_A !== undefined ? payload.parameters.O_A : (payload.parameters.organic_flow_ratio || 0),
          A_O: payload.parameters.A_O !== undefined ? payload.parameters.A_O : (payload.parameters.aqueous_flow_ratio || 0),
        });
        const pp = payload.parameters;
        const D = Number(pp.D_Cu !== undefined ? pp.D_Cu : (pp.D || 0)) || 0;
        const qmax = Number(pp.Qmax_Cu || 0) || 0, K = Number(pp.isotherm_K || 1) || 1, n = Number(pp.isotherm_n || 1) || 1;
        if (D > 0) {
          const pts = [];
          for (let j = 0; j < 31; j++) {
            const ca = j / 30, co = D * ca;
            const q = qmax > 0 ? qmax * (K * Math.pow(co, n)) / (1 + K * Math.pow(co, n)) : co;
            pts.push({ C_aq: ca, C_org_eq: co, loading: q });
          }
          isotherms.push({ node_id: nid, name: payload.name, points: pts });
        }
      }
    }
    return {
      nodes: nodePayload,
      streams: streams.map(plain),
      terminals: terminals.map(plain),
      kpis: this._kpis(terminals),
      solver: { converged: true, iterations: iteration, residual, tolerance: this.tol, relaxation: this.relax, recycle_count: recycle.length },
      sankey: SXEWProcessEngine._sankey(streams),
      trace: this.trace.slice(-500),
      stage_profiles: profiles,
      isotherms,
    };
  }

  _inputsForNode(nid, inc, edgeStreams, guesses) {
    const arr = edgeStreams.filter(([e]) => e.target === nid && !e.recycle).map(([, s]) => s);
    for (const e of (inc[nid] || [])) if (e.recycle) arr.push(guesses.get(eKey(e)));
    return arr.length ? arr : [this.feed.copy({ id: `FEED->${nid}`, target: nid })];
  }

  _validate() {
    if (!Object.keys(this.nodes).length) throw new ProcessError('Circuit has no nodes');
    for (const e of this.edges) {
      if (!(e.source in this.nodes) || !(e.target in this.nodes)) throw new ProcessError(`Invalid edge ${e.source}->${e.target}`);
      if (e.source === e.target) throw new ProcessError('Self-loop is not supported');
      if (e.fraction !== undefined && e.fraction !== null && Number(e.fraction) < 0) throw new ProcessError('Edge fraction cannot be negative');
    }
    for (const n of Object.values(this.nodes)) {
      const p = n.params;
      for (const k of ['efficiency', 'cu_extraction', 'cu_stripping', 'stage_efficiency', 'cu_deposition', 'fe_rejection', 'fe_extraction', 'fe_stripping', 'acid_transfer', 'phase_recovery']) {
        if (k in p && !(pct(p[k]) >= 0 && pct(p[k]) <= 1)) throw new ProcessError(`${n.id}.${k} must be between 0 and 100%`);
      }
      for (const k of ['organic_flow_ratio', 'aqueous_flow_ratio', 'O_A', 'A_O', 'organic_inventory', 'phase_disengagement_time', 'settling_time', 'D_Cu', 'D_Fe', 'D_acid', 'Qmax_Cu', 'isotherm_K', 'isotherm_n']) {
        if (k in p && p[k] < 0) throw new ProcessError(`${n.id}.${k} must be >= 0`);
      }
    }
  }

  _topologicalOrder(edges) {
    const inc = {}, out = {};
    for (const e of edges) {
      (inc[e.target] = inc[e.target] || []).push(e);
      (out[e.source] = out[e.source] || []).push(e);
    }
    const indeg = {};
    for (const n of Object.keys(this.nodes)) indeg[n] = (inc[n] || []).length;
    const q = Object.keys(this.nodes).filter(n => !indeg[n]);
    const order = [];
    while (q.length) {
      const n = q.shift(); order.push(n);
      for (const e of (out[n] || [])) if (--indeg[e.target] === 0) q.push(e.target);
    }
    if (order.length !== Object.keys(this.nodes).length) throw new ProcessError('Circular path found without recycle=true');
    return order;
  }

  _weights(es) {
    if (es.length === 1) return [1.0];
    if (es.some(e => e.fraction === undefined || e.fraction === null)) throw new ProcessError('Parallel branches require fraction on every branch');
    const w = es.map(e => Math.max(0, Number(e.fraction)));
    const total = w.reduce((a, b) => a + b, 0);
    if (total <= 0) throw new ProcessError('Parallel fractions must sum to > 0');
    return w.map(x => x / total);
  }

  _mix(ss, nid) {
    const valid = ss.filter(s => s.flow > EPS);
    const flow = valid.reduce((a, s) => a + s.flow, 0);
    if (flow <= EPS) return stream({ id: `MIX->${nid}`, flow: 0 });
    const masses = {};
    for (const x of SPECIES) masses[x] = valid.reduce((a, s) => a + s.flow * s[x], 0);
    const org = valid.reduce((a, s) => a + (s.phase === 'organic' ? s.flow : 0), 0);
    const aq = flow - org;
    const phase = org >= aq ? 'organic' : 'aqueous';
    return stream({ id: `MIX->${nid}`, flow, Cu: masses.Cu / flow, Fe: masses.Fe / flow, H2SO4: masses.H2SO4 / flow, phase });
  }

  _phaseRecovery(p) {
    if ('phase_recovery' in p) return pct(p.phase_recovery);
    const t = p.phase_disengagement_time !== undefined ? p.phase_disengagement_time : (p.settling_time !== undefined ? p.settling_time : 0);
    const target = p.target_disengagement_time !== undefined ? p.target_disengagement_time : 30;
    if (t <= 0) return 1.0;
    return clamp(1 - Math.exp(-t / Math.max(1e-9, target)));
  }

  _equilibriumExtract(aq, orgFlow, p) {
    const aqM = aq.masses();
    const acid = aq.H2SO4;
    let D = Math.max(0.0, p.D_Cu !== undefined ? p.D_Cu : (p.D !== undefined ? p.D : 0.0));
    if (D <= 0) {
      const eff = pct(p.efficiency !== undefined ? p.efficiency : (p.cu_extraction !== undefined ? p.cu_extraction : 0.0));
      D = eff / (Math.max(EPS, 1 - eff)) * Math.max(EPS, orgFlow / aq.flow);
    }
    const acidRef = Math.max(EPS, p.acid_reference !== undefined ? p.acid_reference : .05);
    const acidExp = p.acid_exponent !== undefined ? p.acid_exponent : 0.0;
    D *= Math.max(EPS, Math.pow(Math.max(EPS, acid) / acidRef, acidExp));
    const oa = orgFlow / Math.max(EPS, aq.flow);
    const frac = D * oa / (1 + D * oa);
    const qmax = Math.max(0.0, p.Qmax_Cu !== undefined ? p.Qmax_Cu : (p.organic_cu_capacity !== undefined ? p.organic_cu_capacity : 0.0));
    const isoK = Math.max(EPS, p.isotherm_K !== undefined ? p.isotherm_K : 1.0);
    const isoN = Math.max(EPS, p.isotherm_n !== undefined ? p.isotherm_n : 1.0);
    const existing = Math.max(0.0, p.initial_Cu_loading !== undefined ? p.initial_Cu_loading : 0.0);
    let cuX;
    if (qmax > 0) {
      let capacity = qmax * orgFlow * Math.max(0.0, 1 - existing / qmax);
      const capFactor = 1 / (1 + isoK * Math.pow(Math.max(existing, 0) / Math.max(qmax, EPS), isoN));
      capacity = Math.max(0.0, capacity * capFactor);
      cuX = Math.min(aqM.Cu * frac, capacity);
    } else {
      cuX = aqM.Cu * frac;
    }
    const feFrac = 'fe_extraction' in p ? clamp(pct(p.fe_extraction) * Math.max(0.0, 1 - frac), 0, 1) : 0.0;
    const feX = aqM.Fe * feFrac;
    const acidX = aqM.H2SO4 * pct(p.acid_transfer !== undefined ? p.acid_transfer : 0.0);
    return { Cu: cuX, Fe: feX, H2SO4: acidX, D_Cu: D, fraction: frac };
  }

  _equilibriumStrip(org, aqFlow, p) {
    const om = org.masses();
    let D = Math.max(0.0, p.D_Cu !== undefined ? p.D_Cu : (p.D !== undefined ? p.D : 0.0));
    if (D <= 0) {
      const eff = pct(p.efficiency !== undefined ? p.efficiency : (p.cu_stripping !== undefined ? p.cu_stripping : 0.0));
      D = Math.max(EPS, (1 - eff) / Math.max(EPS, eff)) * Math.max(EPS, aqFlow / org.flow);
    }
    const oa = aqFlow / Math.max(EPS, org.flow);
    let frac = oa / (Math.max(EPS, D) + oa);
    frac = clamp(frac);
    const dProvided = p.D_Cu !== undefined ? p.D_Cu : (p.D !== undefined ? p.D : 0.0);
    if ('cu_stripping' in p && dProvided <= 0) frac = Math.max(frac, pct(p.cu_stripping));
    const qmax = Math.max(0.0, p.Qmax_Cu !== undefined ? p.Qmax_Cu : 0.0);
    let cuS = om.Cu * frac;
    if (qmax > 0) cuS = Math.min(cuS, om.Cu);
    const feS = om.Fe * pct(p.fe_stripping !== undefined ? p.fe_stripping : 0.0);
    const acidS = om.H2SO4 * pct(p.acid_transfer !== undefined ? p.acid_transfer : 0.0);
    return { Cu: cuS, Fe: feS, H2SO4: acidS, D_Cu: D, fraction: frac };
  }

  _transform(n, streams) {
    const t = n.type.toLowerCase(); const p = n.params;
    if (['feed', 'source', 'mixer', 'mix', 'split', 'splitter'].includes(t)) {
      return { out: this._mix(streams, n.id) };
    }
    if (['ex', 'extraction', 'extractor'].includes(t)) {
      const aq = this._mix(streams.filter(s => s.phase === 'aqueous'), n.id);
      const orgs = streams.filter(s => s.phase === 'organic');
      const orgin = orgs.length ? this._mix(orgs, n.id) : null;
      if (aq.flow <= EPS) return { raffinate: SXEWProcessEngine._zeroPhase('aqueous', n.id), organic: SXEWProcessEngine._zeroPhase('organic', n.id) };
      const oa = p.O_A !== undefined ? p.O_A : (p.organic_flow_ratio !== undefined ? p.organic_flow_ratio : 1.0);
      const orgFlow = orgin && orgin.flow > EPS ? orgin.flow : aq.flow * Math.max(EPS, oa);
      const x = this._equilibriumExtract(aq, orgFlow, p);
      const rec = this._phaseRecovery(p);
      for (const k of SPECIES) x[k] *= rec;
      const am = aq.masses();
      const raff = stream({ id: `${n.id}:raffinate`, flow: aq.flow, Cu: Math.max(0, am.Cu - x.Cu) / aq.flow, Fe: Math.max(0, am.Fe - x.Fe) / aq.flow, H2SO4: Math.max(0, am.H2SO4 - x.H2SO4) / aq.flow, phase: 'aqueous' });
      const om = {};
      for (const k of SPECIES) om[k] = (orgin ? orgin.flow * orgin[k] : 0) + x[k];
      const org = stream({ id: `${n.id}:organic`, flow: orgFlow, Cu: om.Cu / orgFlow, Fe: om.Fe / orgFlow, H2SO4: om.H2SO4 / orgFlow, phase: 'organic' });
      this.trace.push({ node: n.id, type: 'Extraction', D_Cu: x.D_Cu, equilibrium_fraction: x.fraction, phase_recovery: rec, Cu_transfer: x.Cu });
      return { raffinate: raff, organic: org };
    }
    if (['px', 'stripper', 'stripping'].includes(t)) {
      const org = this._mix(streams.filter(s => s.phase === 'organic'), n.id);
      const aq = this._mix(streams.filter(s => s.phase === 'aqueous'), n.id);
      if (org.flow <= EPS) return { electrolyte: SXEWProcessEngine._zeroPhase('aqueous', n.id), lean_organic: SXEWProcessEngine._zeroPhase('organic', n.id) };
      const ao = p.A_O !== undefined ? p.A_O : (p.aqueous_flow_ratio !== undefined ? p.aqueous_flow_ratio : 1.0);
      const aqFlow = aq.flow > EPS ? aq.flow : org.flow * Math.max(EPS, ao);
      const x = this._equilibriumStrip(org, aqFlow, p);
      const rec = this._phaseRecovery(p);
      for (const k of SPECIES) x[k] *= rec;
      const om = org.masses(); const am = aq.masses();
      const elec = stream({ id: `${n.id}:electrolyte`, flow: aqFlow, Cu: (am.Cu + x.Cu) / aqFlow, Fe: (am.Fe + x.Fe) / aqFlow, H2SO4: (am.H2SO4 + x.H2SO4) / aqFlow, phase: 'aqueous' });
      const lean = stream({ id: `${n.id}:lean_organic`, flow: org.flow, Cu: (om.Cu - x.Cu) / org.flow, Fe: (om.Fe - x.Fe) / org.flow, H2SO4: (om.H2SO4 - x.H2SO4) / org.flow, phase: 'organic' });
      this.trace.push({ node: n.id, type: 'Stripper', D_Cu: x.D_Cu, equilibrium_fraction: x.fraction, phase_recovery: rec, Cu_transfer: x.Cu });
      return { electrolyte: elec, lean_organic: lean };
    }
    if (['w', 'wash', 'washing'].includes(t)) {
      const s = this._mix(streams, n.id); const r = pct(p.fe_rejection !== undefined ? p.fe_rejection : 95);
      return { out: s.copy({ Fe: s.Fe * (1 - r) }) };
    }
    if (['stage', 's'].includes(t)) {
      const s = this._mix(streams, n.id); return { out: s };
    }
    if (['ew', 'electro', 'electrowinning'].includes(t)) {
      const s = this._mix(streams, n.id); const dep = pct(p.cu_deposition !== undefined ? p.cu_deposition : 98);
      const productMass = s.flow * s.Cu * dep;
      return { product: stream({ id: `${n.id}:copper_product`, flow: productMass, Cu: 1, Fe: 0, H2SO4: 0, phase: 'solid' }), out: s.copy({ Cu: s.Cu * (1 - dep) }) };
    }
    throw new ProcessError(`Unsupported block type: ${n.type}`);
  }

  static _zero(e) { return stream({ id: `RECYCLE:${e.source}->${e.target}`, flow: 0, phase: 'aqueous', source: e.source, target: e.target, port: e.port }); }
  static _zeroPhase(phase, nid) { return stream({ id: `${nid}:${phase}`, flow: 0, phase }); }
  _relax(a, b) {
    const r = this.relax;
    return a.copy({ flow: a.flow * (1 - r) + b.flow * r, Cu: a.Cu * (1 - r) + b.Cu * r, Fe: a.Fe * (1 - r) + b.Fe * r, H2SO4: a.H2SO4 * (1 - r) + b.H2SO4 * r, phase: b.phase, source: b.source, target: b.target, port: b.port });
  }
  static _delta(a, b) { return Math.max(Math.abs(a.flow - b.flow), Math.abs(a.Cu - b.Cu), Math.abs(a.Fe - b.Fe), Math.abs(a.H2SO4 - b.H2SO4)); }

  _kpis(terms) {
    const feed = this.feed.masses();
    const product = {}; const residualM = {};
    for (const x of SPECIES) {
      product[x] = terms.filter(s => s.phase === 'solid').reduce((a, s) => a + s.flow * s[x], 0);
      residualM[x] = terms.filter(s => s.phase !== 'solid').reduce((a, s) => a + s.flow * s[x], 0);
    }
    const error = {};
    for (const x of SPECIES) error[x] = feed[x] - product[x] - residualM[x];
    return {
      feed_flow: this.feed.flow, feed_Cu_mass: feed.Cu, product_Cu_mass: product.Cu, residual_Cu_mass: residualM.Cu,
      out_flow: terms.reduce((a, s) => a + s.flow, 0),
      Cu_recovery_pct: feed.Cu ? product.Cu / feed.Cu * 100 : 0,
      Cu_balance_error: error.Cu,
      mass_balance_error: Math.max(...SPECIES.map(x => Math.abs(error[x]))),
      mass_balance: { feed: feed, product: product, residual: residualM, error: error },
    };
  }
  static _sankey(streams) {
    return streams.filter(s => s.source && s.target && s.flow > EPS).map(s => ({
      source: s.source, target: s.target, value: s.flow, phase: s.phase,
      Cu_mass: s.flow * s.Cu, Fe_mass: s.flow * s.Fe, acid_mass: s.flow * s.H2SO4, port: s.port, Cu_pct: s.Cu * 100,
    }));
  }
}

// ---------- Optimizer (port of app/optimizer.py) ----------
const OBJECTIVES = {
  max_recovery: ['max', 'Cu_recovery_pct'],
  max_product_cu: ['max', 'product_Cu_mass'],
  max_capacity: ['max', 'capacity_flow'],
  min_oa: ['min', 'total_OA'],
  min_stages: ['min', 'stage_count'],
  min_acid: ['min', 'acid_out_mass'],
  min_organic_inventory: ['min', 'organic_inventory'],
  min_cost: ['min', 'total_cost'],
  min_balance_error: ['min', 'mass_balance_error'],
};

function _resolve(c, parts) {
  let cur = c; let i = 0;
  while (i < parts.length) {
    const p = parts[i];
    if (Array.isArray(cur)) {
      if (i > 0 && parts[i - 1] === 'nodes') {
        cur = cur.find(n => String(n.id) === p);
        if (cur === undefined) throw new Error(p);
      } else { cur = cur[parseInt(p, 10)]; }
    } else { cur = cur[p]; }
    i += 1;
  }
  return cur;
}
const _get = (c, path) => _resolve(c, path.split('.'));
function _set(c, path, val) {
  const parts = path.split('.'); let cur = c;
  for (let i = 0; i < parts.length - 1; i++) {
    const p = parts[i];
    if (Array.isArray(cur)) {
      if (i > 0 && parts[i - 1] === 'nodes') cur = cur.find(n => String(n.id) === p);
      else cur = cur[parseInt(p, 10)];
    } else { cur = cur[p]; }
  }
  const last = parts[parts.length - 1];
  if (Array.isArray(cur)) cur[parseInt(last, 10)] = val; else cur[last] = val;
}

function _metrics(c, r) {
  const k = r.kpis; const nodes = c.nodes || []; const feed = c.feed || {};
  const oa = nodes.reduce((a, n) => a + Number((n.params || {}).O_A !== undefined ? n.params.O_A : ((n.params || {}).organic_flow_ratio || 0)) || 0, 0);
  const stages = nodes.reduce((a, n) => a + (['ex', 'px', 'extraction', 'extractor', 'stripper', 'stripping'].includes(String(n.type || '').toLowerCase()) ? (parseInt((n.params || {}).stage_count || 1, 10) || 1) : 0), 0);
  const acidOut = (r.terminals || []).reduce((a, s) => a + Number(s.flow || 0) * Number(s.H2SO4 || 0), 0);
  const organicInventory = nodes.reduce((a, n) => a + (Number((n.params || {}).organic_inventory || 0) || 0), 0);
  const caps = nodes.map(n => Number((n.params || {}).capacity_flow || 0)).filter(v => v > 0);
  const capacityFlow = caps.length ? Math.min(...caps) : Number(feed.flow || 0);
  const econ = ((c.optimization || {}).economics) || {};
  const fixed = Number(econ.fixed_cost_per_stage || 0);
  const flowCost = Number(econ.variable_cost_per_flow || 0);
  const orgCost = Number(econ.organic_inventory_cost || 0);
  const acidCost = Number(econ.acid_cost_per_mass || 0);
  const power = Number(econ.power_cost_per_product_cu || 0);
  const totalCost = fixed * stages + flowCost * Number(feed.flow || 0) + orgCost * organicInventory + acidCost * acidOut + power * Number(k.product_Cu_mass || 0);
  return {
    Cu_recovery_pct: k.Cu_recovery_pct !== undefined ? k.Cu_recovery_pct : 0,
    product_Cu_mass: k.product_Cu_mass !== undefined ? k.product_Cu_mass : 0,
    mass_balance_error: k.mass_balance_error !== undefined ? k.mass_balance_error : 0,
    total_OA: oa, stage_count: stages, acid_out_mass: acidOut, organic_inventory: organicInventory,
    capacity_flow: capacityFlow, total_cost: totalCost,
    iterations: r.solver.iterations, residual: r.solver.residual,
  };
}

function _dominates(a, b, objectives) {
  let better = false;
  for (const o of objectives) {
    const [d, m] = OBJECTIVES[o.objective];
    const av = Number(a[m]); const bv = Number(b[m]);
    if (d === 'max') {
      if (av < bv) return false;
      if (av > bv) better = true;
    } else {
      if (av > bv) return false;
      if (av < bv) better = true;
    }
  }
  return better;
}

function _pareto(rows, objectives) {
  const valid = rows.filter(r => r.metrics && r.score !== undefined && r.score !== null);
  const front = [];
  valid.forEach((a, i) => {
    if (!valid.some((b, j) => j !== i && _dominates(b.metrics, a.metrics, objectives))) front.push(a);
  });
  return front.sort((x, y) => x.evaluation - y.evaluation);
}

function _crowding(front, objectives) {
  if (front.length <= 2) { const d = new Map(); for (const x of front) d.set(x, Infinity); return d; }
  const dist = new Map(front.map(x => [x, 0.0]));
  for (const o of objectives) {
    const m = OBJECTIVES[o.objective][1];
    const ordered = [...front].sort((a, b) => Number(a.metrics[m]) - Number(b.metrics[m]));
    const lo = Number(ordered[0].metrics[m]); const hi = Number(ordered[ordered.length - 1].metrics[m]);
    dist.set(ordered[0], Infinity); dist.set(ordered[ordered.length - 1], Infinity);
    if (hi === lo) continue;
    for (let i = 1; i < ordered.length - 1; i++) {
      dist.set(ordered[i], dist.get(ordered[i]) + Math.abs(Number(ordered[i + 1].metrics[m]) - Number(ordered[i - 1].metrics[m])) / (hi - lo));
    }
  }
  return dist;
}

function _normalizeScore(m, objectives) {
  let s = 0;
  for (const o of objectives) {
    const [d, key] = OBJECTIVES[o.objective];
    const w = Number(o.weight !== undefined ? o.weight : 1);
    const v = Number(m[key]); const scale = Math.max(Math.abs(v), 1.0);
    s += w * (d === 'max' ? v / scale : -v / scale);
  }
  return s;
}

// Seeded PRNG (mulberry32) so JS and Python use their own reproducible streams.
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function optimize(circuit, variables, objectives, maxEvals = 240, tolerance = 1e-8, populationSize = 24, seed = 42) {
  if (!variables.length) throw new OptimizationError('حداقل یک متغیر برای بهینه‌سازی تعریف کنید');
  if (!objectives || !objectives.length) objectives = [{ objective: 'max_recovery', weight: 1 }];
  for (const o of objectives) if (!OBJECTIVES[o.objective]) throw new OptimizationError(`هدف نامعتبر: ${o.objective}`);
  const base = JSON.parse(JSON.stringify(circuit));
  const rng = mulberry32(seed >>> 0);
  for (const v of variables) {
    if (!('path' in v)) throw new OptimizationError('مسیر متغیر مشخص نشده است');
    const lo = Number(v.min); const hi = Number(v.max); const step = Number(v.step || 0) || 0;
    if (hi < lo || step <= 0) throw new OptimizationError(`بازه/گام متغیر ${v.path} نامعتبر است`);
    try { _get(base, v.path); } catch (e) { throw new OptimizationError(`متغیر ${v.path} در مدار وجود ندارد`); }
  }
  const popn = Math.max(8, Math.min(64, parseInt(populationSize, 10)));
  maxEvals = Math.max(popn, parseInt(maxEvals, 10));
  const history = []; let evals = 0;
  function gauss() { // Box–Muller
    let u = 0, v = 0;
    while (u === 0) u = rng();
    while (v === 0) v = rng();
    return Math.sqrt(-2.0 * Math.log(u)) * Math.cos(2.0 * Math.PI * v);
  }
  function makeInd(values) {
    const c = JSON.parse(JSON.stringify(base)); const vals = {};
    for (const v of variables) {
      const lo = Number(v.min); const hi = Number(v.max);
      let x = values && Object.prototype.hasOwnProperty.call(values, v.path) ? Number(values[v.path]) : lo + (hi - lo) * rng();
      if (String(v.kind || 'float').toLowerCase() === 'int' || String(v.kind || 'float').toLowerCase() === 'integer') x = Math.round(x);
      x = Math.max(lo, Math.min(hi, x));
      _set(c, v.path, x); vals[v.path] = x;
    }
    return [c, vals];
  }
  function evaluate(c, vals, tag) {
    if (evals >= maxEvals) return null;
    try {
      const r = new SXEWProcessEngine(c).run();
      const m = _metrics(c, r);
      const row = { evaluation: evals + 1, tag, variables: Object.assign({}, vals), metrics: m, score: _normalizeScore(m, objectives) };
      history.push(row); evals += 1; return row;
    } catch (e) {
      if (!(e instanceof ProcessError)) throw e;
      evals += 1;
      history.push({ evaluation: evals + 1, tag, variables: Object.assign({}, vals), metrics: null, score: null, error: String(e.message || e) });
      return null;
    }
  }
  const population = [];
  const evaluateBase = evaluate(base, Object.fromEntries(variables.map(v => [v.path, _get(base, v.path)])), 'baseline');
  if (evaluateBase) population.push(evaluateBase);
  while (population.length < popn && evals < maxEvals) {
    const [c, vals] = makeInd();
    const row = evaluate(c, vals, 'initial');
    if (row) population.push(row);
  }
  let generations = 0;
  while (evals < maxEvals && population.length) {
    generations += 1;
    const front = _pareto(population, objectives);
    const crowd = _crowding(front, objectives);
    const parents = front.slice();
    const ranked = [...population].sort((a, b) =>
      ((front.includes(a) ? 0 : 1) - (front.includes(b) ? 0 : 1)) || (-(crowd.get(a) || 0) + (crowd.get(b) || 0)) || (b.score - a.score));
    parents.push(...ranked);
    const parentPool = parents.slice(0, Math.max(4, popn));
    const offspring = [];
    while (offspring.length < popn && evals < maxEvals) {
      const a = parentPool[Math.floor(rng() * parentPool.length)];
      const b = parentPool[Math.floor(rng() * parentPool.length)];
      const vals = {};
      for (const v of variables) {
        const lo = Number(v.min); const hi = Number(v.max);
        const av = Number(a.variables[v.path]); const bv = Number(b.variables[v.path]);
        const alpha = rng();
        let x = alpha * av + (1 - alpha) * bv;
        const span = hi - lo;
        if (span) x += gauss() * 0.08 * span;
        if (rng() < 0.12) x = lo + (hi - lo) * rng();
        if (String(v.kind || 'float').toLowerCase() === 'int' || String(v.kind || 'float').toLowerCase() === 'integer') x = Math.round(x);
        vals[v.path] = Math.max(lo, Math.min(hi, x));
      }
      const [c, fixedVals] = makeInd(vals);
      const row = evaluate(c, fixedVals, 'nsga2');
      if (row) offspring.push(row);
    }
    let pool = population.concat(offspring).filter(x => x.metrics);
    const nue = [];
    while (pool.length && nue.length < popn) {
      const f = pool.filter(x => !pool.some(y => y !== x && _dominates(y.metrics, x.metrics, objectives)));
      if (!f.length) break;
      if (nue.length + f.length <= popn) {
        nue.push(...f); pool = pool.filter(x => !f.includes(x));
      } else {
        const cd = _crowding(f, objectives);
        f.sort((a, b) => (cd.get(b) || 0) - (cd.get(a) || 0));
        nue.push(...f.slice(0, popn - nue.length)); break;
      }
    }
    population.length = 0; population.push(...nue);
    if (generations >= 50) break;
  }
  const front = _pareto(history, objectives);
  let best = front[0] || evaluateBase;
  if (best) {
    const bc = JSON.parse(JSON.stringify(base));
    for (const v of variables) _set(bc, v.path, best.variables[v.path]);
    best = JSON.parse(JSON.stringify(best)); best.circuit = bc;
  }
  return {
    algorithm: 'NSGA-II style bounded multi-objective evolutionary search',
    seed, evaluations: evals, max_evals: maxEvals, generations,
    objectives, variables, pareto_front: front, pareto_count: front.length, best,
    history: history.slice(-300),
  };
}

// ---------- small helpers ----------
function plain(s) {
  const { copy, masses, ...rest } = s;
  return { source: null, target: null, port: 'out', ...rest };
}
function mapValues(obj, fn) { const o = {}; for (const [k, v] of Object.entries(obj)) o[k] = fn(v); return o; }
function sum(arr, fn) { return arr.reduce((a, x) => a + fn(x), 0); }

/* Export for Node tests / bundlers while exposing globals for the static site. */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { SXEWProcessEngine, optimize, ProcessError, OptimizationError, OBJECTIVES, pct, clamp };
}
if (typeof window !== 'undefined') {
  window.SXEW = { SXEWProcessEngine, optimize, ProcessError, OptimizationError, pct, clamp };
}
