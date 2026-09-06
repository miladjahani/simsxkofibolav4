from __future__ import annotations
from dataclasses import dataclass, asdict
from collections import defaultdict, deque
from typing import Any
import math

SPECIES=("Cu","Fe","H2SO4")
EPS=1e-12

@dataclass
class Stream:
    id:str
    flow:float=0.0
    Cu:float=0.0
    Fe:float=0.0
    H2SO4:float=0.0
    phase:str="aqueous"
    source:str|None=None
    target:str|None=None
    port:str="out"
    def copy(self,**kw):
        d=asdict(self); d.update(kw); return Stream(**d)
    def masses(self):
        return {x:max(0.0,self.flow*getattr(self,x)) for x in SPECIES}

@dataclass
class Node:
    id:str; name:str; type:str; params:dict[str,float]
@dataclass
class Edge:
    source:str; target:str; mode:str="series"; fraction:float|None=None; port:str="out"; recycle:bool=False

class ProcessError(Exception): pass

class SXEWProcessEngine:
    """Domain-level SX-EW simulator.

    Streams carry flow plus Cu/Fe/acid mass fractions. Extraction and stripping
    are phase-aware and conserve component mass. Recycle edges are solved by
    fixed-point iteration with relaxation; convergence and residual are reported.
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
        for e in circuit.get("edges",[]):
            if not isinstance(e,dict): e={"source":e[0],"target":e[1],"mode":e[2] if len(e)>2 else "series","fraction":e[3] if len(e)>3 else None,"port":e[4] if len(e)>4 else "out"}
            self.edges.append(Edge(e["source"],e["target"],e.get("mode","series"),e.get("fraction"),e.get("port","out"),bool(e.get("recycle",False))))
        f=circuit.get("feed",{})
        self.feed=Stream("FEED",float(f.get("flow",1000)),float(f.get("Cu",.02)),float(f.get("Fe",.01)),float(f.get("H2SO4",.05)),f.get("phase","aqueous"))
        self.max_iter=int(circuit.get("solver",{}).get("max_iter",250)); self.tol=float(circuit.get("solver",{}).get("tolerance",1e-8)); self.relax=float(circuit.get("solver",{}).get("relaxation",.65))
        self.streams=[]; self.node_results={}; self.solver={}

    def run(self):
        self._validate(); inc=defaultdict(list); out=defaultdict(list)
        for e in self.edges: inc[e.target].append(e); out[e.source].append(e)
        recycle=[e for e in self.edges if e.recycle]
        nonrec=[e for e in self.edges if not e.recycle]
        order=self._topological_order(nonrec)
        # Previous recycle guesses are explicit stream objects.
        guesses={e_key(e): self._zero_stream(e) for e in recycle}
        final_nodes={}; final_streams=[]; converged=not recycle; residual=float("inf")
        for it in range(1,self.max_iter+1):
            arriving=defaultdict(list); produced_by={}; all_edge_streams=[]
            roots=[nid for nid in order if not [e for e in inc[nid] if not e.recycle]]
            for nid in roots:
                # A Feed node gets the global feed; otherwise a root receives it.
                if self.nodes[nid].type.lower() in ("feed","source"):
                    arriving[nid]=[self.feed.copy(id=f"FEED->{nid}",target=nid)]
                else:
                    arriving[nid]=[self.feed.copy(id=f"FEED->{nid}",target=nid)]
            for nid in order:
                # Add recycle arrivals from previous iteration.
                for e in inc[nid]:
                    if e.recycle: arriving[nid].append(guesses[e_key(e)].copy(target=nid))
                ins=arriving[nid]
                inp=self._mix(ins,nid)
                produced=self._transform(self.nodes[nid],inp)
                produced_by[nid]=produced
                if out[nid]:
                    for port,base in produced.items():
                        es=[e for e in out[nid] if e.port==port or (e.port=="out" and port=="out")]
                        if not es: continue
                        weights=self._weights(es)
                        for e,w in zip(es,weights):
                            s=base.copy(id=f"{nid}:{port}->{e.target}",flow=base.flow*w,source=nid,target=e.target,port=port)
                            all_edge_streams.append((e,s))
                            if e.recycle:
                                old=guesses[e_key(e)]; guesses[e_key(e)]=self._relax(old,s)
                            else: arriving[e.target].append(s)
            new_guess=guesses
            residual=max((self._stream_delta(new_guess[e_key(e)], guesses[e_key(e)]) for e in recycle),default=0.0)
            # Because guesses were relaxed in-place, compute convergence from node/output fingerprints.
            if recycle:
                # recompute residual against source streams generated in this iteration
                residual=max((self._stream_delta(guesses[e_key(e)], next((s for ee,s in all_edge_streams if ee is e), self._zero_stream(e))) for e in recycle),default=0.0)
                converged=residual<=self.tol
            if converged or not recycle:
                final_nodes=produced_by; final_streams=all_edge_streams; break
        if recycle and not converged:
            raise ProcessError(f"Recycle solver did not converge after {self.max_iter} iterations (residual={residual:.3e}). Increase max_iter, change relaxation, or inspect recycle topology.")
        self.node_results={nid:{"input":asdict(self._mix(self._node_inputs(nid,inc,final_streams,guesses),nid)),"outputs":{k:asdict(v) for k,v in p.items()}} for nid,p in final_nodes.items()}
        terminals=[]
        for e,s in final_streams:
            if not out[e.target]:
                terminals.append(s.copy(id=f"{s.id}->OUT",target="OUT"))
        self.streams=[s for _,s in final_streams]+terminals
        self.solver={"converged":True,"iterations":it,"residual":residual,"tolerance":self.tol,"relaxation":self.relax,"recycle_count":len(recycle)}
        return {"nodes":self.node_results,"streams":[asdict(s) for s in self.streams],"terminals":[asdict(s) for s in terminals],"kpis":self._kpis(terminals,final_nodes),"solver":self.solver,"sankey":self._sankey(self.streams)}

    def _node_inputs(self,nid,inc,final_streams,guesses):
        arr=[]
        for e,s in final_streams:
            if e.target==nid and not e.recycle: arr.append(s)
        for e in inc[nid]:
            if e.recycle: arr.append(guesses[e_key(e)])
        if not arr: arr=[self.feed.copy(id=f"FEED->{nid}",target=nid)]
        return arr

    def _validate(self):
        if not self.nodes: raise ProcessError("Circuit has no nodes")
        for e in self.edges:
            if e.source not in self.nodes or e.target not in self.nodes: raise ProcessError(f"Invalid edge {e.source}->{e.target}")
            if e.source==e.target: raise ProcessError("Self-loop is not supported")
        for n in self.nodes.values():
            p=n.params
            for k in ("efficiency","cu_extraction","cu_stripping","stage_efficiency","cu_deposition","fe_rejection"):
                if k in p and not 0<=p[k]<=1 and k not in ("cu_extraction","cu_stripping"): raise ProcessError(f"{n.id}.{k} must be between 0 and 1")
            if "organic_flow_ratio" in p and p["organic_flow_ratio"]<=0: raise ProcessError(f"{n.id}.organic_flow_ratio must be > 0")

    def _topological_order(self,edges):
        inc=defaultdict(list); out=defaultdict(list)
        for e in edges: inc[e.target].append(e); out[e.source].append(e)
        indeg={n:len(inc[n]) for n in self.nodes}; q=deque(n for n in self.nodes if indeg[n]==0); order=[]
        while q:
            n=q.popleft(); order.append(n)
            for e in out[n]:
                indeg[e.target]-=1
                if indeg[e.target]==0:q.append(e.target)
        if len(order)!=len(self.nodes): raise ProcessError("A circular path exists without a recycle=true edge")
        return order

    def _weights(self,es):
        if len(es)==1:return [1.0]
        if any(e.fraction is None for e in es): raise ProcessError("Parallel branches require an explicit fraction on every branch")
        w=[max(0,float(e.fraction)) for e in es]; total=sum(w)
        if total<=0: raise ProcessError("Parallel branch fractions must sum to > 0")
        if total>1+1e-9: w=[x/total for x in w]
        return w

    def _mix(self,ss,nid):
        valid=[s for s in ss if s.flow>EPS]
        flow=sum(s.flow for s in valid)
        if flow<=EPS:return Stream(f"MIX->{nid}",0)
        kw={x:sum(s.flow*getattr(s,x) for s in valid)/flow for x in SPECIES}
        org=sum(s.flow for s in valid if s.phase=="organic")>=sum(s.flow for s in valid if s.phase=="aqueous")
        return Stream(f"MIX->{nid}",flow,phase="organic" if org else "aqueous",**kw)

    def _transform(self,n,s):
        p=n.params; t=n.type.lower()
        if t in ("feed","source","mixer","mix","split","splitter"): return {"out":s}
        if t in ("ex","extraction","extractor"):
            eff=self._frac(p.get("efficiency",p.get("cu_extraction",.90)))
            oar=max(EPS,p.get("organic_flow_ratio",1.0)); org_flow=max(s.flow*oar,p.flow if hasattr(p,"flow") else 0.0)
            # If an organic recycle enters, retain its flow; otherwise generate make-up organic.
            if s.phase=="organic":
                aq_flow=max(EPS,s.flow/oar); aq=s
            else:
                aq_flow=s.flow; aq=s
                org_flow=max(org_flow, s.flow*oar)
            cu_mass=aq_flow*aq.Cu; fe_mass=aq_flow*aq.Fe; acid_mass=aq_flow*aq.H2SO4
            fe_ext=self._frac(p.get("fe_extraction",0.0))*fe_mass
            extracted=min(cu_mass*eff, max(0.0,org_flow*p.get("organic_cu_capacity",1.0)))
            raff=Stream(f"{n.id}:raffinate",aq_flow,(cu_mass-extracted)/aq_flow,(fe_mass-fe_ext)/aq_flow,acid_mass/aq_flow,"aqueous")
            org=Stream(f"{n.id}:organic",org_flow,extracted/org_flow,fe_ext/org_flow,0.0,"organic")
            return {"raffinate":raff,"organic":org}
        if t in ("px","stripper","stripping"):
            eff=self._frac(p.get("efficiency",p.get("cu_stripping",.90))); aq_ratio=max(EPS,p.get("aqueous_flow_ratio",1.0))
            if s.phase!="organic":
                org=s; org_flow=s.flow; aq_flow=max(EPS,s.flow*aq_ratio)
            else: org=s; org_flow=s.flow; aq_flow=max(EPS,s.flow*aq_ratio)
            cu=s.flow*s.Cu; extracted=cu*eff
            fe_strip=cu*0 + s.flow*s.Fe*self._frac(p.get("fe_stripping",0.0))
            elec=Stream(f"{n.id}:electrolyte",aq_flow,extracted/aq_flow,fe_strip/aq_flow,s.H2SO4,"aqueous")
            lean=Stream(f"{n.id}:lean_organic",org_flow,max(0, s.Cu*(1-eff)),max(0,s.Fe-self._frac(p.get("fe_stripping",0.0))*s.Fe),s.H2SO4,"organic")
            return {"electrolyte":elec,"lean_organic":lean}
        if t in ("w","wash","washing"):
            rej=self._frac(p.get("fe_rejection",.95)); return {"out":s.copy(Fe=s.Fe*(1-rej))}
        if t in ("stage","s"):
            eff=self._frac(p.get("efficiency",p.get("stage_efficiency",.95))); return {"out":s.copy(Cu=s.Cu*eff)}
        if t in ("ew","electro","electrowinning"):
            dep=self._frac(p.get("cu_deposition",.98)); product_mass=s.flow*s.Cu*dep
            product=Stream(f"{n.id}:copper_product",product_mass,1,0,0,"solid")
            return {"product":product,"out":s.copy(Cu=s.Cu*(1-dep))}
        raise ProcessError(f"Unsupported block type: {n.type}")

    @staticmethod
    def _frac(v):
        v=float(v); return v/100 if v>1 else max(0,min(1,v))
    @staticmethod
    def _zero_stream(e): return Stream(f"RECYCLE:{e.source}->{e.target}",0,0,0,0,"aqueous",e.source,e.target,e.port)
    def _relax(self,old,new):
        a=self.relax
        return old.copy(flow=old.flow*(1-a)+new.flow*a,Cu=old.Cu*(1-a)+new.Cu*a,Fe=old.Fe*(1-a)+new.Fe*a,H2SO4=old.H2SO4*(1-a)+new.H2SO4*a,phase=new.phase,source=new.source,target=new.target,port=new.port)
    @staticmethod
    def _stream_delta(a,b):
        return max(abs(a.flow-b.flow),abs(a.Cu-b.Cu),abs(a.Fe-b.Fe),abs(a.H2SO4-b.H2SO4))

    def _kpis(self,terms,nodes):
        feed={x:self.feed.flow*getattr(self.feed,x) for x in SPECIES}
        product={x:sum(s.flow*getattr(s,x) for s in terms if s.phase=="solid") for x in SPECIES}
        residual={x:sum(s.flow*getattr(s,x) for s in terms if s.phase!="solid") for x in SPECIES}
        err={x:feed[x]-product[x]-residual[x] for x in SPECIES}
        return {"feed_flow":self.feed.flow,"feed_Cu_mass":feed["Cu"],"product_Cu_mass":product["Cu"],"residual_Cu_mass":residual["Cu"],"out_flow":sum(s.flow for s in terms),"Cu_recovery_pct":product["Cu"]/feed["Cu"]*100 if feed["Cu"] else 0,"Cu_balance_error":err["Cu"],"mass_balance_error":max(abs(v) for v in err.values()),"mass_balance":{"feed":feed,"product":product,"residual":residual,"error":err}}

    @staticmethod
    def _sankey(streams):
        links=[]
        for s in streams:
            if s.source and s.target and s.flow>EPS:
                links.append({"source":s.source,"target":s.target,"value":s.flow,"phase":s.phase,"Cu_mass":s.flow*s.Cu,"Fe_mass":s.flow*s.Fe,"acid_mass":s.flow*s.H2SO4,"port":s.port})
        return links

def e_key(e:Edge): return f"{e.source}|{e.target}|{e.port}|{id(e)}"
