from __future__ import annotations
from dataclasses import dataclass, asdict
from collections import defaultdict, deque
from typing import Any
import math

SPECIES=("Cu","Fe","H2SO4")
EPS=1e-12

@dataclass
class Stream:
    id:str; flow:float=0.0; Cu:float=0.0; Fe:float=0.0; H2SO4:float=0.0; phase:str="aqueous"; source:str|None=None; target:str|None=None; port:str="out"
    def copy(self,**kw): d=asdict(self); d.update(kw); return Stream(**d)
    def masses(self): return {x:self.flow*getattr(self,x) for x in SPECIES}

@dataclass
class Node:
    id:str; name:str; type:str; params:dict[str,float]
@dataclass
class Edge:
    source:str; target:str; mode:str="series"; fraction:float|None=None; port:str="out"; recycle:bool=False
class ProcessError(Exception): pass

def e_key(e:Edge): return f"{e.source}|{e.target}|{e.port}|{id(e)}"

def pct(v):
    v=float(v); return v/100.0 if v>1 else max(0.0,min(1.0,v))

def clamp(v,a=0.0,b=1.0): return max(a,min(b,float(v)))

class SXEWProcessEngine:
    """Stage-by-stage empirical SX-EW engine.

    Species are represented as mass fractions and all transfers are performed on mass,
    preserving component balances. Extraction uses a distribution coefficient/isotherm
    relationship and finite organic inventory/capacity. Stripping uses an analogous
    empirical D and equilibrium split. Phase disengagement applies a recovery factor.
    Recycle edges are solved with damped fixed-point iteration.
    """
    def __init__(self,circuit:dict[str,Any]):
        self.nodes={}
        for n in circuit.get("nodes",[]):
            params={}
            for k,v in n.get("params",{}).items():
                try: params[k]=float(v)
                except: pass
            self.nodes[n["id"]]=Node(n["id"],n.get("name",n["id"]),n.get("type","Stage"),params)
        self.edges=[]
        for raw in circuit.get("edges",[]):
            e=raw if isinstance(raw,dict) else {"source":raw[0],"target":raw[1],"mode":raw[2] if len(raw)>2 else "series","fraction":raw[3] if len(raw)>3 else None,"port":raw[4] if len(raw)>4 else "out"}
            self.edges.append(Edge(e["source"],e["target"],e.get("mode","series"),e.get("fraction"),e.get("port","out"),bool(e.get("recycle",False))))
        f=circuit.get("feed",{})
        self.feed=Stream("FEED",float(f.get("flow",1000)),float(f.get("Cu",.02)),float(f.get("Fe",.01)),float(f.get("H2SO4",.05)),f.get("phase","aqueous"))
        solver=circuit.get("solver",{})
        self.max_iter=max(1,int(solver.get("max_iter",500))); self.tol=max(1e-12,float(solver.get("tolerance",1e-8))); self.relax=clamp(solver.get("relaxation",.55),.05,.99)
        self.trace=[]

    def run(self):
        self._validate()
        inc=defaultdict(list); out=defaultdict(list)
        for e in self.edges: inc[e.target].append(e); out[e.source].append(e)
        recycle=[e for e in self.edges if e.recycle]
        order=self._topological_order([e for e in self.edges if not e.recycle])
        guesses={e_key(e):self._zero(e) for e in recycle}
        residual=math.inf; produced_by={}; edge_streams=[]; iteration=0
        for iteration in range(1,self.max_iter+1):
            arriving=defaultdict(list); produced_by={}; edge_streams=[]
            for nid in order:
                incoming=[s for e,s in edge_streams if e.target==nid and not e.recycle]
                incoming += [guesses[e_key(e)].copy(target=nid) for e in inc[nid] if e.recycle]
                if not incoming and (not inc[nid] or not any(not e.recycle for e in inc[nid])):
                    incoming=[self.feed.copy(id=f"FEED->{nid}",target=nid)]
                arriving[nid]=incoming
                produced=self._transform(self.nodes[nid],incoming); produced_by[nid]=produced
                for port,base in produced.items():
                    es=[e for e in out[nid] if e.port==port or (e.port=="out" and port=="out")]
                    if not es: continue
                    for e,w in zip(es,self._weights(es)):
                        s=base.copy(id=f"{nid}:{port}->{e.target}",flow=base.flow*w,source=nid,target=e.target,port=port)
                        edge_streams.append((e,s))
            residual=0.0
            for e in recycle:
                new=next((s for ee,s in edge_streams if ee is e),self._zero(e)); old=guesses[e_key(e)]
                residual=max(residual,self._delta(old,new)); guesses[e_key(e)]=self._relax(old,new)
            if not recycle or (residual<=self.tol*0.01 and iteration>=2): break
        if recycle and residual>self.tol*0.01: raise ProcessError(f"Recycle solver did not converge after {self.max_iter} iterations (residual={residual:.3e})")

        terminals=[]
        for nid,produced in produced_by.items():
            for port,s in produced.items():
                es=[e for e in out[nid] if e.port==port or (e.port=="out" and port=="out")]
                if not es: terminals.append(s.copy(id=f"{nid}:{port}->OUT",source=nid,target="OUT",port=port))
        streams=[s for e,s in edge_streams if not e.recycle]
        streams += [guesses[e_key(e)] for e in recycle if guesses[e_key(e)].flow>EPS]
        streams += terminals
        node_payload={}
        for nid,prod in produced_by.items():
            inp=self._mix(self._inputs_for_node(nid,inc,edge_streams,guesses),nid)
            node_payload[nid]={"name":self.nodes[nid].name,"type":self.nodes[nid].type,"input":asdict(inp),"outputs":{k:asdict(v) for k,v in prod.items()},"parameters":self.nodes[nid].params}
        profiles=[]; isotherms=[]
        for nid,payload in node_payload.items():
            typ=str(payload["type"]).lower()
            if typ in ("ex","extraction","extractor","px","stripper","stripping"):
                inp=payload.get("input",{}); outs=payload.get("outputs",{})
                profiles.append({"stage":len(profiles)+1,"node_id":nid,"name":payload["name"],"type":"Extraction" if typ.startswith("ex") or typ=="extractor" else "Stripper","feed_flow":inp.get("flow",0),"Cu_in":inp.get("Cu",0),"Fe_in":inp.get("Fe",0),"acid_in":inp.get("H2SO4",0),"Cu_out":sum(v.get("flow",0)*v.get("Cu",0) for v in outs.values()),"Fe_out":sum(v.get("flow",0)*v.get("Fe",0) for v in outs.values()),"acid_out":sum(v.get("flow",0)*v.get("H2SO4",0) for v in outs.values()),"D_Cu":payload["parameters"].get("D_Cu",payload["parameters"].get("D",0)),"O_A":payload["parameters"].get("O_A",payload["parameters"].get("organic_flow_ratio",0)),"A_O":payload["parameters"].get("A_O",payload["parameters"].get("aqueous_flow_ratio",0))})
                pp=payload["parameters"]; D=float(pp.get("D_Cu",pp.get("D",0)) or 0); qmax=float(pp.get("Qmax_Cu",0) or 0); K=float(pp.get("isotherm_K",1) or 1); n=float(pp.get("isotherm_n",1) or 1)
                if D>0:
                    pts=[]
                    for j in range(31):
                        ca=j/30
                        co=D*ca
                        q=qmax*(K*co**n)/(1+K*co**n) if qmax>0 else co
                        pts.append({"C_aq":ca,"C_org_eq":co,"loading":q})
                    isotherms.append({"node_id":nid,"name":payload["name"],"points":pts})
        return {"nodes":node_payload,"streams":[asdict(s) for s in streams],"terminals":[asdict(s) for s in terminals],"kpis":self._kpis(terminals),"solver":{"converged":True,"iterations":iteration,"residual":residual,"tolerance":self.tol,"relaxation":self.relax,"recycle_count":len(recycle)},"sankey":self._sankey(streams),"trace":self.trace[-500:],"stage_profiles":profiles,"isotherms":isotherms}

    def _inputs_for_node(self,nid,inc,edge_streams,guesses):
        arr=[s for e,s in edge_streams if e.target==nid and not e.recycle]
        arr += [guesses[e_key(e)] for e in inc[nid] if e.recycle]
        return arr or [self.feed.copy(id=f"FEED->{nid}",target=nid)]

    def _validate(self):
        if not self.nodes: raise ProcessError("Circuit has no nodes")
        for e in self.edges:
            if e.source not in self.nodes or e.target not in self.nodes: raise ProcessError(f"Invalid edge {e.source}->{e.target}")
            if e.source==e.target: raise ProcessError("Self-loop is not supported")
            if e.fraction is not None and float(e.fraction)<0: raise ProcessError("Edge fraction cannot be negative")
        for n in self.nodes.values():
            p=n.params
            for k in ("efficiency","cu_extraction","cu_stripping","stage_efficiency","cu_deposition","fe_rejection","fe_extraction","fe_stripping","acid_transfer","phase_recovery"):
                if k in p and not 0<=pct(p[k])<=1: raise ProcessError(f"{n.id}.{k} must be between 0 and 100%")
            for k in ("organic_flow_ratio","aqueous_flow_ratio","O_A","A_O","organic_inventory","phase_disengagement_time","settling_time","D_Cu","D_Fe","D_acid","Qmax_Cu","isotherm_K","isotherm_n"):
                if k in p and p[k]<0: raise ProcessError(f"{n.id}.{k} must be >= 0")

    def _topological_order(self,edges):
        inc=defaultdict(list); out=defaultdict(list)
        for e in edges: inc[e.target].append(e); out[e.source].append(e)
        indeg={n:len(inc[n]) for n in self.nodes}; q=deque(n for n in self.nodes if indeg[n]==0); order=[]
        while q:
            n=q.popleft(); order.append(n)
            for e in out[n]:
                indeg[e.target]-=1
                if indeg[e.target]==0:q.append(e.target)
        if len(order)!=len(self.nodes): raise ProcessError("Circular path found without recycle=true")
        return order

    def _weights(self,es):
        if len(es)==1:return [1.0]
        if any(e.fraction is None for e in es): raise ProcessError("Parallel branches require fraction on every branch")
        w=[max(0,float(e.fraction)) for e in es]; total=sum(w)
        if total<=0: raise ProcessError("Parallel fractions must sum to > 0")
        return [x/total for x in w]

    def _mix(self,ss,nid):
        ss=[s for s in ss if s.flow>EPS]; flow=sum(s.flow for s in ss)
        if flow<=EPS:return Stream(f"MIX->{nid}",0)
        masses={x:sum(s.flow*getattr(s,x) for s in ss) for x in SPECIES}
        phase="organic" if sum(s.flow for s in ss if s.phase=="organic")>=sum(s.flow for s in ss if s.phase=="aqueous") else "aqueous"
        return Stream(f"MIX->{nid}",flow,*(masses[x]/flow for x in SPECIES),phase)

    def _phase_recovery(self,p):
        if "phase_recovery" in p:return pct(p["phase_recovery"])
        t=p.get("phase_disengagement_time",p.get("settling_time",0)); target=p.get("target_disengagement_time",30)
        if t<=0:return 1.0
        # Empirical hydraulic disengagement curve: approaches 1 as residence/settling time increases.
        return clamp(1-math.exp(-t/max(1e-9,target)))

    def _distribution(self,p,species,phase="extraction"):
        acid=max(EPS,p.get("H2SO4_ref",p.get("acid_reference",0.05)))
        h2so4=max(0.0,p.get("acid_override",0.0))
        # If acid_override is supplied it is the stage acid concentration; otherwise caller's value is used later.
        base=p.get("D_"+species,p.get("D_Cu",0.0) if species=="Cu" else p.get("D_Fe",0.0))
        if base<=0:
            base=p.get("distribution_coefficient",p.get("D",0.0))
        acid_factor=p.get("acid_factor",0.0)
        if h2so4>0 and acid_factor:
            base*=math.exp(acid_factor*(h2so4-acid))
        return max(0.0,base)

    def _equilibrium_extract(self,aq,org_flow,p):
        aq_m=aq.masses(); org_in=0.0
        # Cu distribution coefficient can be dynamically acid dependent.
        acid=aq.H2SO4
        D=max(0.0,p.get("D_Cu",p.get("D",0.0)))
        if D<=0:
            # fallback to the legacy efficiency only if no D was supplied
            eff=pct(p.get("efficiency",p.get("cu_extraction",0.0)))
            D=eff/(max(EPS,1-eff))*max(EPS,org_flow/aq.flow)
        acid_ref=max(EPS,p.get("acid_reference",.05)); acid_exp=p.get("acid_exponent",0.0)
        D*=max(EPS,(max(EPS,acid)/acid_ref)**acid_exp)
        oa=org_flow/max(EPS,aq.flow)
        frac=D*oa/(1+D*oa)
        # Isotherm loading limits equilibrium concentration in organic phase.
        qmax=max(0.0,p.get("Qmax_Cu",p.get("organic_cu_capacity",0.0)))
        isoK=max(EPS,p.get("isotherm_K",1.0)); isoN=max(EPS,p.get("isotherm_n",1.0))
        existing=max(0.0, p.get("initial_Cu_loading",0.0))
        if qmax>0:
            capacity=qmax*org_flow*max(0.0,1-existing/qmax)
            # Optional Langmuir/Freundlich capacity modifier
            cap_factor=1/(1+isoK*(max(existing,0)/max(qmax,EPS))**isoN)
            capacity=max(0.0,capacity*cap_factor)
            cu_x=min(aq_m["Cu"]*frac,capacity)
        else: cu_x=aq_m["Cu"]*frac
        fe_frac=clamp(pct(p.get("fe_extraction",0.0))*max(0.0,1-frac),0,1) if "fe_extraction" in p else 0.0
        fe_x=aq_m["Fe"]*fe_frac
        acid_x=aq_m["H2SO4"]*pct(p.get("acid_transfer",0.0))
        return {"Cu":cu_x,"Fe":fe_x,"H2SO4":acid_x,"D_Cu":D,"fraction":frac}

    def _equilibrium_strip(self,org,aq_flow,p):
        om=org.masses(); D=max(0.0,p.get("D_Cu",p.get("D",0.0)))
        if D<=0:
            eff=pct(p.get("efficiency",p.get("cu_stripping",0.0))); D=max(EPS,(1-eff)/max(EPS,eff))*max(EPS,aq_flow/org.flow)
        oa=aq_flow/max(EPS,org.flow)
        # D = C_org/C_aq. Fraction stripped = (O/A)/(D + O/A).
        frac=(oa)/(max(EPS,D)+oa)
        frac=clamp(frac)
        if "cu_stripping" in p and p.get("D_Cu",p.get("D",0.0))<=0: frac=max(frac,pct(p["cu_stripping"]))
        qmax=max(0.0,p.get("Qmax_Cu",0.0))
        cu_s=om["Cu"]*frac
        if qmax>0: cu_s=min(cu_s,om["Cu"])
        fe_s=om["Fe"]*pct(p.get("fe_stripping",0.0))
        acid_s=om["H2SO4"]*pct(p.get("acid_transfer",0.0))
        return {"Cu":cu_s,"Fe":fe_s,"H2SO4":acid_s,"D_Cu":D,"fraction":frac}

    def _transform(self,n,streams):
        t=n.type.lower(); p=n.params
        if t in ("feed","source","mixer","mix","split","splitter"):
            return {"out":self._mix(streams,n.id)}
        if t in ("ex","extraction","extractor"):
            aq=self._mix([s for s in streams if s.phase=="aqueous"],n.id); orgs=[s for s in streams if s.phase=="organic"]; orgin=self._mix(orgs,n.id) if orgs else None
            if aq.flow<=EPS:return {"raffinate":self._zero_phase("aqueous",n.id),"organic":self._zero_phase("organic",n.id)}
            oa=p.get("O_A",p.get("organic_flow_ratio",1.0)); org_flow=orgin.flow if orgin and orgin.flow>EPS else aq.flow*max(EPS,oa)
            x=self._equilibrium_extract(aq,org_flow,p); rec=self._phase_recovery(p)
            # A phase-recovery loss leaves both component and flow reduced proportionally.
            for k in SPECIES: x[k]*=rec
            am=aq.masses(); raff=Stream(f"{n.id}:raffinate",aq.flow,*(max(0,am[k]-x[k])/aq.flow for k in SPECIES),"aqueous")
            om={k:(orgin.flow*getattr(orgin,k) if orgin else 0)+x[k] for k in SPECIES}
            org=Stream(f"{n.id}:organic",org_flow,*(om[k]/org_flow for k in SPECIES),"organic")
            self.trace.append({"node":n.id,"type":"Extraction","D_Cu":x["D_Cu"],"equilibrium_fraction":x["fraction"],"phase_recovery":rec,"Cu_transfer":x["Cu"]})
            return {"raffinate":raff,"organic":org}
        if t in ("px","stripper","stripping"):
            org=self._mix([s for s in streams if s.phase=="organic"],n.id); aq=self._mix([s for s in streams if s.phase=="aqueous"],n.id)
            if org.flow<=EPS:return {"electrolyte":self._zero_phase("aqueous",n.id),"lean_organic":self._zero_phase("organic",n.id)}
            ao=p.get("A_O",p.get("aqueous_flow_ratio",1.0)); aq_flow=aq.flow if aq.flow>EPS else org.flow*max(EPS,ao)
            x=self._equilibrium_strip(org,aq_flow,p); rec=self._phase_recovery(p)
            for k in SPECIES:x[k]*=rec
            om=org.masses(); am=aq.masses()
            elec=Stream(f"{n.id}:electrolyte",aq_flow,*((am[k]+x[k])/aq_flow for k in SPECIES),"aqueous")
            lean=Stream(f"{n.id}:lean_organic",org.flow,*((om[k]-x[k])/org.flow for k in SPECIES),"organic")
            self.trace.append({"node":n.id,"type":"Stripper","D_Cu":x["D_Cu"],"equilibrium_fraction":x["fraction"],"phase_recovery":rec,"Cu_transfer":x["Cu"]})
            return {"electrolyte":elec,"lean_organic":lean}
        if t in ("w","wash","washing"):
            s=self._mix(streams,n.id); r=pct(p.get("fe_rejection",95)); return {"out":s.copy(Fe=s.Fe*(1-r))}
        if t in ("stage","s"):
            s=self._mix(streams,n.id); return {"out":s}
        if t in ("ew","electro","electrowinning"):
            s=self._mix(streams,n.id); dep=pct(p.get("cu_deposition",98)); product_mass=s.flow*s.Cu*dep
            return {"product":Stream(f"{n.id}:copper_product",product_mass,1,0,0,"solid"),"out":s.copy(Cu=s.Cu*(1-dep))}
        raise ProcessError(f"Unsupported block type: {n.type}")

    @staticmethod
    def _zero(e): return Stream(f"RECYCLE:{e.source}->{e.target}",0,0,0,0,"aqueous",e.source,e.target,e.port)
    @staticmethod
    def _zero_phase(phase,nid): return Stream(f"{nid}:{phase}",0,0,0,0,phase)
    def _relax(self,a,b):
        r=self.relax; return a.copy(flow=a.flow*(1-r)+b.flow*r,Cu=a.Cu*(1-r)+b.Cu*r,Fe=a.Fe*(1-r)+b.Fe*r,H2SO4=a.H2SO4*(1-r)+b.H2SO4*r,phase=b.phase,source=b.source,target=b.target,port=b.port)
    @staticmethod
    def _delta(a,b): return max(abs(a.flow-b.flow),abs(a.Cu-b.Cu),abs(a.Fe-b.Fe),abs(a.H2SO4-b.H2SO4))

    def _kpis(self,terms):
        feed=self.feed.masses(); product={x:sum(s.flow*getattr(s,x) for s in terms if s.phase=="solid") for x in SPECIES}; residual={x:sum(s.flow*getattr(s,x) for s in terms if s.phase!="solid") for x in SPECIES}; error={x:feed[x]-product[x]-residual[x] for x in SPECIES}
        return {"feed_flow":self.feed.flow,"feed_Cu_mass":feed["Cu"],"product_Cu_mass":product["Cu"],"residual_Cu_mass":residual["Cu"],"out_flow":sum(s.flow for s in terms),"Cu_recovery_pct":product["Cu"]/feed["Cu"]*100 if feed["Cu"] else 0,"Cu_balance_error":error["Cu"],"mass_balance_error":max(abs(x) for x in error.values()),"mass_balance":{"feed":feed,"product":product,"residual":residual,"error":error}}
    @staticmethod
    def _sankey(streams):
        return [{"source":s.source,"target":s.target,"value":s.flow,"phase":s.phase,"Cu_mass":s.flow*s.Cu,"Fe_mass":s.flow*s.Fe,"acid_mass":s.flow*s.H2SO4,"port":s.port,"Cu_pct":s.Cu*100} for s in streams if s.source and s.target and s.flow>EPS]
