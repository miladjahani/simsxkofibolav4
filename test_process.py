from app.process_engine import SXEWProcessEngine

def circuit():
    nodes=[{"id":"F","type":"Feed"},{"id":"E1","type":"Ex","params":{"efficiency":90,"organic_flow_ratio":1}},{"id":"E2","type":"Ex","params":{"efficiency":92,"organic_flow_ratio":1}},{"id":"P","type":"Px","params":{"efficiency":95}},{"id":"EW","type":"EW","params":{"cu_deposition":98}}]
    edges=[{"source":"F","target":"E1","port":"out"},{"source":"E1","target":"E2","port":"raffinate"},{"source":"E2","target":"P","port":"organic"},{"source":"P","target":"EW","port":"electrolyte"},{"source":"P","target":"E2","port":"lean_organic","recycle":True}]
    return {"feed":{"flow":1000,"Cu":.02,"Fe":.01,"H2SO4":.05},"nodes":nodes,"edges":edges,"solver":{"max_iter":500,"tolerance":1e-9,"relaxation":.65}}

def test_recycle_converges_and_cu_balance():
    r=SXEWProcessEngine(circuit()).run()
    assert r["solver"]["converged"]
    assert r["solver"]["iterations"] > 1
    assert abs(r["kpis"]["mass_balance"]["error"]["Cu"]) < 1e-7

def test_sankey_contains_recycle_network():
    r=SXEWProcessEngine(circuit()).run()
    assert any(x["source"]=="P" and x["target"]=="E2" for x in r["sankey"])

def test_parallel_requires_fractions():
    c=circuit(); c["edges"].extend([{"source":"F","target":"E2"}])
    try: SXEWProcessEngine(c).run()
    except Exception as e: assert "fraction" in str(e).lower()

def test_multiobjective_pareto_engine():
    from app.optimizer import optimize
    c=circuit()
    c['nodes'][1]['params']['D_Cu']=2.5; c['nodes'][1]['params']['O_A']=1.0; c['nodes'][1]['params']['organic_inventory']=10; c['nodes'][1]['params']['capacity_flow']=1500
    vars=[{'path':'nodes.E1.params.D_Cu','min':1.0,'max':5.0,'step':0.2},{'path':'nodes.E1.params.O_A','min':0.5,'max':2.0,'step':0.1}]
    obs=[{'objective':'max_recovery'},{'objective':'min_cost'},{'objective':'max_capacity'}]
    r=optimize(c,vars,obs,max_evals=30,population_size=8,seed=7)
    assert r['pareto_count'] >= 1
    assert r['algorithm'].startswith('NSGA-II')
