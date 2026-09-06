"""Native Excel-formula execution engine for the supplied SX-EW workbook.
Supports the formula vocabulary actually present in the workbook (arithmetic, cell
references, LOG and IF), with a dependency graph and cycle detection.
"""
from __future__ import annotations
import ast, math, re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import openpyxl

CELL_RE = re.compile(r"(?<![A-Z0-9_])\$?([A-Z]{1,3})\$?(\d+)(?![A-Z0-9_])")
SHEET_CELL_RE = re.compile(r"(?:'([^']+)'|([A-Za-z0-9_ ]+))!\$?([A-Z]{1,3})\$?(\d+)")

class FormulaError(Exception): pass

class _Safe:
    def __init__(self, cell_fn): self.cell_fn=cell_fn
    def eval(self, expr:str):
        expr=expr.strip()
        if expr.startswith('='): expr=expr[1:]
        # Excel exponent operator
        expr=expr.replace('^','**')
        # normalize absolute refs; cross-sheet references are represented as cell("Sheet!A1")
        def sheet_sub(m):
            sheet=(m.group(1) or m.group(2)).replace('"','\\"')
            return f'cell("{sheet}!{m.group(3)}{m.group(4)}")'
        expr=SHEET_CELL_RE.sub(sheet_sub,expr)
        expr=CELL_RE.sub(lambda m:f'cell("{m.group(1)}{m.group(2)}")',expr)
        expr=re.sub(r'(?<![<>=])=(?!=)','==',expr)
        expr=re.sub(r'<>','!=',expr)
        expr=re.sub(r'\bTRUE\b','True',expr,flags=re.I)
        expr=re.sub(r'\bFALSE\b','False',expr,flags=re.I)
        expr=re.sub(r'\bLOG10\s*\(', 'log10(', expr, flags=re.I)
        expr=re.sub(r'\bLOG\s*\(', 'log(', expr, flags=re.I)
        expr=re.sub(r'\bABS\s*\(', 'abs(', expr, flags=re.I)
        expr=re.sub(r'\bSQRT\s*\(', 'sqrt(', expr, flags=re.I)
        expr=re.sub(r'\bMIN\s*\(', 'min(', expr, flags=re.I)
        expr=re.sub(r'\bMAX\s*\(', 'max(', expr, flags=re.I)
        expr=re.sub(r'\bIF\s*\(', 'if_((', expr, flags=re.I)
        # Fix IF's extra opening parenthesis: IF(a,b,c) -> if_(a,b,c)
        if 'if_((' in expr:
            expr=expr.replace('if_((', 'if_(', 1)
        tree=ast.parse(expr,mode='eval')
        self._check(tree)
        env={'cell':self.cell_fn,'log':lambda x:math.log10(x),'log10':lambda x:math.log10(x),
             'abs':abs,'sqrt':math.sqrt,'min':min,'max':max,
             'if_':lambda cond,a,b: a if cond else b,'True':True,'False':False}
        try: return eval(compile(tree,'<excel>','eval'),{'__builtins__':{}},env)
        except Exception as e: raise FormulaError(f'{expr}: {e}') from e
    def _check(self,node):
        allowed=(ast.Expression,ast.BinOp,ast.UnaryOp,ast.BoolOp,ast.Compare,ast.Call,ast.Name,ast.Constant,
                 ast.Add,ast.Sub,ast.Mult,ast.Div,ast.Pow,ast.Mod,ast.USub,ast.UAdd,ast.And,ast.Or,
                 ast.Eq,ast.NotEq,ast.Lt,ast.LtE,ast.Gt,ast.GtE,ast.Load)
        if not isinstance(node,allowed): raise FormulaError(f'Unsupported Excel expression: {type(node).__name__}')
        if isinstance(node,ast.Call):
            if not isinstance(node.func,ast.Name) or node.func.id not in {'cell','log','log10','abs','sqrt','min','max','if_'}:
                raise FormulaError('Unsupported function')
        for c in ast.iter_child_nodes(node): self._check(c)

@dataclass
class NativeWorkbook:
    path: Path
    def __post_init__(self):
        self.wb=openpyxl.load_workbook(self.path,data_only=False,read_only=False)
    def calculate(self,sheet:str,inputs:dict[str,Any]|None=None,only_cells:list[str]|None=None):
        if sheet not in self.wb.sheetnames: raise FormulaError(f'Unknown sheet: {sheet}')
        ws=self.wb[sheet]
        values={}
        formulas={}
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    if isinstance(c.value,str) and c.value.startswith('='): formulas[c.coordinate]=c.value
                    else: values[c.coordinate]=c.value
        for k,v in (inputs or {}).items(): values[k.upper().replace('$','')]=v
        state={}
        def get(ref):
            # cross-sheet ref
            if '!' in ref:
                sh,coord=ref.split('!',1); sh=sh.strip("'")
                if sh not in self.wb.sheetnames: raise FormulaError(f'Unknown sheet reference {sh}')
                c=self.wb[sh][coord]
                if isinstance(c.value,str) and c.value.startswith('='):
                    return self._calc_sheet_cell(sh,coord,inputs or {},{})
                return c.value if c.value is not None else 0
            ref=ref.upper().replace('$','')
            if ref in values: return values[ref]
            if ref in state: return state[ref]
            if ref not in formulas: return 0
            if state.get(ref,'__missing__')=='__busy__': raise FormulaError(f'Circular reference at {sheet}!{ref}')
            state[ref]='__busy__'
            val=_Safe(get).eval(formulas[ref])
            if val is None: val=0
            state[ref]=val
            return val
        engine=_Safe(get)
        targets=only_cells or list(formulas.keys())
        for coord in targets:
            if coord in formulas: get(coord)
        out={k:(get(k) if k in formulas else v) for k,v in values.items()}
        out.update({k:get(k) for k in targets if k in formulas})
        return {'sheet':sheet,'values':out,'formulas':{k:formulas[k] for k in targets if k in formulas},'engine':'native-python'}
    def _calc_sheet_cell(self,sh,coord,inputs,cache):
        # Independent recursive evaluator for cross-sheet dependencies.
        ws=self.wb[sh]; values={}; formulas={}
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    if isinstance(c.value,str) and c.value.startswith('='): formulas[c.coordinate]=c.value
                    else: values[c.coordinate]=c.value
        values.update({k.upper().replace('$',''):v for k,v in inputs.items() if '!' not in k})
        state={}
        def get(r):
            r=r.upper().replace('$','')
            if r in values:return values[r]
            if r in state:return state[r]
            if r not in formulas:return 0
            state[r]=_Safe(get).eval(formulas[r]); return state[r]
        return get(coord)
