from __future__ import annotations
from copy import deepcopy
from typing import Any
import random, math
from .process_engine import SXEWProcessEngine, ProcessError

OBJECTIVES={
    'max_recovery': ('max','Cu_recovery_pct'),
    'max_product_cu': ('max','product_Cu_mass'),
    'max_capacity': ('max','capacity_flow'),
    'min_oa': ('min','total_OA'),
    'min_stages': ('min','stage_count'),
    'min_acid': ('min','acid_out_mass'),
    'min_organic_inventory': ('min','organic_inventory'),
    'min_cost': ('min','total_cost'),
    'min_balance_error': ('min','mass_balance_error'),
}

class OptimizationError(Exception): pass

def _resolve(c,parts):
    cur=c; i=0
    while i<len(parts):
        p=parts[i]
        if isinstance(cur,list):
            if i>0 and parts[i-1]=='nodes':
                cur=next((n for n in cur if n.get('id')==p),None)
                if cur is None: raise KeyError(p)
            else: cur=cur[int(p)]
        else: cur=cur[p]
        i+=1
    return cur

def _get(c,path): return _resolve(c,path.split('.'))

def _set(c,path,val):
    parts=path.split('.'); cur=c
    for i,p in enumerate(parts[:-1]):
        if isinstance(cur,list):
            if i>0 and parts[i-1]=='nodes': cur=next(n for n in cur if n.get('id')==p)
            else: cur=cur[int(p)]
        else: cur=cur[p]
    if isinstance(cur,list): cur[int(parts[-1])]=val
    else: cur[parts[-1]]=val

def _metrics(c,r):
    k=r['kpis']; nodes=c.get('nodes',[]); feed=c.get('feed',{})
    oa=sum(float(n.get('params',{}).get('O_A',n.get('params',{}).get('organic_flow_ratio',0)) or 0) for n in nodes)
    stages=sum(int(n.get('params',{}).get('stage_count',1) or 1) for n in nodes if str(n.get('type','')).lower() in ('ex','px','extraction','extractor','stripper','stripping'))
    acid_out=sum(float(s.get('flow',0))*float(s.get('H2SO4',0)) for s in r.get('terminals',[]))
    organic_inventory=sum(float(n.get('params',{}).get('organic_inventory',0) or 0) for n in nodes)
    caps=[float(n.get('params',{}).get('capacity_flow',0)) for n in nodes if float(n.get('params',{}).get('capacity_flow',0) or 0)>0]
    capacity_flow=min(caps) if caps else float(feed.get('flow',0))
    econ=c.get('optimization',{}).get('economics',{})
    fixed=float(econ.get('fixed_cost_per_stage',0)); flow_cost=float(econ.get('variable_cost_per_flow',0)); org_cost=float(econ.get('organic_inventory_cost',0)); acid_cost=float(econ.get('acid_cost_per_mass',0)); power=float(econ.get('power_cost_per_product_cu',0))
    total_cost=fixed*stages+flow_cost*float(feed.get('flow',0))+org_cost*organic_inventory+acid_cost*acid_out+power*float(k.get('product_Cu_mass',0))
    return {'Cu_recovery_pct':k.get('Cu_recovery_pct',0),'product_Cu_mass':k.get('product_Cu_mass',0),'mass_balance_error':k.get('mass_balance_error',0),'total_OA':oa,'stage_count':stages,'acid_out_mass':acid_out,'organic_inventory':organic_inventory,'capacity_flow':capacity_flow,'total_cost':total_cost,'iterations':r['solver']['iterations'],'residual':r['solver']['residual']}

def _dominates(a,b,objectives):
    better=False
    for o in objectives:
        d,m=OBJECTIVES[o['objective']]; av=float(a[m]); bv=float(b[m])
        if d=='max':
            if av < bv: return False
            if av > bv: better=True
        else:
            if av > bv: return False
            if av < bv: better=True
    return better

def _pareto(rows,objectives):
    valid=[r for r in rows if r.get('metrics') and r.get('score') is not None]
    front=[]
    for i,a in enumerate(valid):
        if not any(j!=i and _dominates(b['metrics'],a['metrics'],objectives) for j,b in enumerate(valid)):
            front.append(a)
    return sorted(front,key=lambda r: r['evaluation'])

def _crowding(front,objectives):
    if len(front)<=2:return {id(x):float('inf') for x in front}
    dist={id(x):0.0 for x in front}
    for o in objectives:
        _,m=OBJECTIVES[o['objective']]; ordered=sorted(front,key=lambda x:float(x['metrics'][m])); lo=float(ordered[0]['metrics'][m]); hi=float(ordered[-1]['metrics'][m]); dist[id(ordered[0])]=dist[id(ordered[-1])]=float('inf')
        if hi==lo:continue
        for i in range(1,len(ordered)-1): dist[id(ordered[i])]+=abs(float(ordered[i+1]['metrics'][m])-float(ordered[i-1]['metrics'][m]))/(hi-lo)
    return dist

def _normalize_score(m,objectives):
    # diagnostic scalar only; Pareto membership never depends on this score.
    s=0
    for o in objectives:
        d,key=OBJECTIVES[o['objective']]; w=float(o.get('weight',1)); v=float(m[key]); scale=max(abs(v),1.0)
        s += w*(v/scale if d=='max' else -v/scale)
    return s

def optimize(circuit:dict[str,Any], variables:list[dict[str,Any]], objectives:list[dict[str,Any]], max_evals:int=240, tolerance:float=1e-8, population_size:int=24, seed:int=42):
    if not variables: raise OptimizationError('حداقل یک متغیر برای بهینه‌سازی تعریف کنید')
    if not objectives: objectives=[{'objective':'max_recovery','weight':1}]
    for o in objectives:
        if o.get('objective') not in OBJECTIVES: raise OptimizationError(f"هدف نامعتبر: {o.get('objective')}")
    base=deepcopy(circuit); rng=random.Random(seed)
    for v in variables:
        if 'path' not in v: raise OptimizationError('مسیر متغیر مشخص نشده است')
        lo=float(v['min']); hi=float(v['max']); step=float(v.get('step',0) or 0)
        if hi<lo or step<=0: raise OptimizationError(f"بازه/گام متغیر {v['path']} نامعتبر است")
        try:_get(base,v['path'])
        except Exception as e:raise OptimizationError(f"متغیر {v['path']} در مدار وجود ندارد") from e
    popn=max(8,min(64,int(population_size))); max_evals=max(popn,int(max_evals)); history=[]; evals=0
    def make_ind(values=None):
        c=deepcopy(base)
        vals={}
        for v in variables:
            lo,hi=float(v['min']),float(v['max'])
            x=float(values[v['path']]) if values and v['path'] in values else (lo+(hi-lo)*rng.random())
            if str(v.get('kind','float')).lower() in ('int','integer'):
                x=round(x)
            x=max(lo,min(hi,x)); _set(c,v['path'],x); vals[v['path']]=x
        return c,vals
    def evaluate(c,vals,tag):
        nonlocal evals
        if evals>=max_evals:return None
        try:
            r=SXEWProcessEngine(c).run(); m=_metrics(c,r); row={'evaluation':evals+1,'tag':tag,'variables':vals.copy(),'metrics':m,'score':_normalize_score(m,objectives)}; history.append(row); evals+=1; return row
        except ProcessError as e:
            evals+=1; history.append({'evaluation':evals,'tag':tag,'variables':vals.copy(),'metrics':None,'score':None,'error':str(e)}); return None
    population=[]
    evaluate_base=evaluate(base,{v['path']:_get(base,v['path']) for v in variables},'baseline')
    population.append(evaluate_base) if evaluate_base else None
    while len(population)<popn and evals<max_evals:
        c,vals=make_ind(); row=evaluate(c,vals,'initial');
        if row:population.append(row)
    generations=0
    while evals<max_evals and population:
        generations+=1
        front=_pareto(population,objectives)
        # parents: Pareto rank first, then crowding distance.
        crowd=_crowding(front,objectives); parents=front[:]
        ranked=sorted(population,key=lambda r:(0 if r in front else 1,-crowd.get(id(r),0),-r['score']))
        parents=(parents+ranked)[:max(4,popn)]
        offspring=[]
        while len(offspring)<popn and evals<max_evals:
            a,b=rng.choice(parents),rng.choice(parents); vals={}
            for v in variables:
                lo,hi=float(v['min']),float(v['max']); av=float(a['variables'][v['path']]); bv=float(b['variables'][v['path']]); alpha=rng.random(); x=alpha*av+(1-alpha)*bv
                span=hi-lo; x += rng.gauss(0,0.08*span) if span else 0
                if rng.random()<0.12: x=lo+(hi-lo)*rng.random()
                if str(v.get('kind','float')).lower() in ('int','integer'): x=round(x)
                vals[v['path']]=max(lo,min(hi,x))
            c,_=make_ind(vals); row=evaluate(c,vals,'nsga2');
            if row: offspring.append(row)
        pool=[x for x in population+offspring if x.get('metrics')]
        new=[]
        while pool and len(new)<popn:
            f=[]
            for x in pool:
                if not any(y is not x and _dominates(y['metrics'],x['metrics'],objectives) for y in pool):f.append(x)
            if len(new)+len(f)<=popn:new.extend(f); pool=[x for x in pool if x not in f]
            else:
                cd=_crowding(f,objectives); f.sort(key=lambda x:cd.get(id(x),0),reverse=True); new.extend(f[:popn-len(new)]); break
        population=new
        if generations>=50:break
    front=_pareto(history,objectives)
    best=front[0] if front else evaluate_base
    # Attach the actual circuit represented by the best variables.
    if best:
        bc=deepcopy(base)
        for v in variables:_set(bc,v['path'],best['variables'][v['path']])
        best=deepcopy(best); best['circuit']=bc
    return {'algorithm':'NSGA-II style bounded multi-objective evolutionary search','seed':seed,'evaluations':evals,'max_evals':max_evals,'generations':generations,'objectives':objectives,'variables':variables,'pareto_front':front,'pareto_count':len(front),'best':best,'history':history[-300:]}
