# Author: David Kong
# License: MIT

"""The flow CSV: one row per spx module and step, plus carbon columns.

CSV layout
----------
seq        Sort key. Rows are processed in ascending seq order, so a step can be
           inserted between 120 and 130 as e.g. 125 (decimals are fine).
row_type   "module" or "step".
flow, module, step
           SPX location names. A module row defines the module (and its
           Comment); step rows name the module they belong to. SPX runs modules
           in module-row order and the steps of each module in seq order.
           spx needs distinct step names within a module: a repeated name, or
           one CarbonEDA uses for the steps it adds (save_tdr_NNNN, save_tbson),
           is renamed <step>_2, <step>_3, ... with a warning.
step_type  The spx.procedural function, e.g. depo_isotropic, etch_anisotropic.
route      Optional: the spx route (wafer) of the row, for a CSV that builds several
           wafers, e.g. a CMOS wafer that a later bond_structure step bonds on top
           (its BondTopStep cell then just names the route). Routes run in the order
           they first appear; a blank cell, or no column, is the tech.yaml route.
signature.<Param>
           Sent to spx as a separate spx.procedural.signature(<Param>=...) call on the
           step, right after it (e.g. signature.AddCustomProperty), not as a step parameter.
carboneda_*
           Carbon columns, never passed to spx. A value on a module row is
           inherited by that module's steps unless the step sets its own.
             carboneda_process_area  the step's process area, a key of
                                     energy_per_step_kwh in config/carbon_config.yaml
                                     (Deposition, Dry_etch, Wet_etch, Metallization,
                                     Wafer; for litho steps the tool: EUV, ArF-i, ArF,
                                     KrF, Hg)
             carboneda_energy_kwh    override the energy per wafer for this step
other columns
           One column per SPX parameter, named exactly as in SPX. An empty cell
           means the parameter is not set (SPX default). Cell syntax:
             quantity            0.0064 um min^-1       (value, then unit)
             *Filter             Nitride|Photoresist    (OR-list of 'Material equals' clauses)
                                 *                      (every material: py:[[]])
                                 Name=Nitride|Oxide     (OR-list of 'Name equals' clauses)
             MaterialDependent   Nitride|Photoresist => 0.04 um min^-1; Silicon => 0.01 um min^-1
             LayerNames          1:0|2:0
             True / False        a boolean, e.g. Flip (py:True works too)
             text                as-is
             anything else       py:<Python literal>    (emitted verbatim)
New parameter columns may be added; their cells are read as a quantity when
they look like "<number> <unit>", otherwise as text (or use py:). Columns may be
in any order, and a column nobody uses can be left out; tidy() (python -m
carbon_eda fmt) writes the standard layout. technologies/README.md has the
format and the examples.
"""

import ast
import csv
import itertools
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field

STRING_COLS = {'Comment', 'Polarity', 'Algorithm', 'ContactsLayerName'}
LIST_COLS = {'LayerNames'}
FIXED_COLS = ['seq', 'row_type', 'flow', 'module', 'step', 'step_type']
ROUTE_COL = 'route'
CARBON_COLS = ['carboneda_process_area', 'carboneda_energy_kwh']
CARBON_PREFIX = 'carboneda_'
SIGNATURE_PREFIX = 'signature.'
# Most-edited parameters first; the rest follow alphabetically.
PREFERRED_COLS = ['Comment', 'BondTopStep', 'Flip', 'MaterialFilter', 'Rate', 'Time', 'MaterialDependent',
                  'Thickness', 'Angle', 'LayerNames', 'Polarity']
# Step names generate.py adds to every module (SAVE_STEP, CHECKPOINT_STEP).
RESERVED_STEP_RE = re.compile(r'save_tdr_\d+|save_tbson')
BOOLS = {'True': True, 'False': False}
QTY_RE = re.compile(r'^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s+(\S.*?)\s*$')


class Unit:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, Unit) and other.name == self.name

    def __repr__(self):
        return f'qupy.Unit({self.name!r})'


class Raw:
    """Python source text emitted verbatim (from a py: cell)."""
    def __init__(self, text):
        self.text = text

    def __eq__(self, other):
        return isinstance(other, Raw) and other.text == self.text


# ---------------------------------------------------------------- Python literals

def literal(node):
    """ast.literal_eval plus qupy.Unit('...') calls."""
    if isinstance(node, ast.Call) and ast.unparse(node.func) == 'qupy.Unit':
        return Unit(ast.literal_eval(node.args[0]))
    if isinstance(node, ast.Tuple):
        return tuple(literal(e) for e in node.elts)
    if isinstance(node, ast.List):
        return [literal(e) for e in node.elts]
    if isinstance(node, ast.Dict):
        return {literal(k): literal(v) for k, v in zip(node.keys, node.values)}
    return ast.literal_eval(node)


def pyrepr(v):
    if isinstance(v, Raw):
        return v.text
    if isinstance(v, tuple):
        inner = ', '.join(pyrepr(e) for e in v)
        return f'({inner},)' if len(v) == 1 else f'({inner})'
    if isinstance(v, list):
        return '[' + ', '.join(pyrepr(e) for e in v) + ']'
    if isinstance(v, dict):
        return '{' + ', '.join(f'{pyrepr(k)}: {pyrepr(x)}' for k, x in v.items()) + '}'
    return repr(v)


def emit_call(func, loc, params, indent='    '):
    args = ''.join(f'{k}={pyrepr(v)},' for k, v in sorted(params.items()))
    return f'{indent}spx.procedural.{func}(Location={loc!r}, {args})\n'


# ---------------------------------------------------------------- cell codec

FILTER_KEYS = ('Material', 'Name')
ANY = [[]]   # a filter with one empty AND-group: every material


def _dec_qty(text):
    m = QTY_RE.match(text)
    return (float(m.group(1)), Unit(m.group(2))) if m else None


def _dec_filter(text):
    if text.strip() == '*':
        return [[]]
    key, sep, names = text.partition('=')
    if not sep or key.strip() not in FILTER_KEYS:
        key, names = 'Material', text
    return [[(key.strip(), 'equals', name.strip())] for name in names.split('|')]


def decode(col, text):
    if text.startswith('py:'):
        return Raw(text[3:].strip())
    if col == 'MaterialDependent':
        out = []
        for entry in text.split(';'):
            filt, rate = entry.split('=>')
            qty = _dec_qty(rate)
            if qty is None:
                raise ValueError(f'bad rate {rate.strip()!r} in MaterialDependent {text!r}')
            out.append((_dec_filter(filt), qty))
        return out
    if col.endswith('Filter'):
        return _dec_filter(text)
    if col in LIST_COLS:
        return text.split('|')
    if col in STRING_COLS:
        return text
    if text in BOOLS:
        return BOOLS[text]
    qty = _dec_qty(text)
    return qty if qty is not None else text


def resolve(v):
    """Decoded cell value with py: cells parsed back into Python values (None if not a literal)."""
    if not isinstance(v, Raw):
        return v
    try:
        return literal(ast.parse(v.text, mode='eval').body)
    except (SyntaxError, ValueError):
        return None


# ---------------------------------------------------------------- flow model

def slug(text, n=48):
    return re.sub(r'[^A-Za-z0-9]+', '_', text).strip('_')[:n] or 'x'


@dataclass
class Module:
    flow: str
    name: str
    params: dict
    carbon: dict
    route: str = ''     # '' is the tech.yaml route


@dataclass
class Step:
    index: int          # 1-based, in the order spx runs the steps
    seq: float
    flow: str
    module: str
    name: str
    step_type: str
    params: dict        # spx parameters, decoded
    cells: dict         # spx parameter cells exactly as written in the CSV
    carbon: dict        # carboneda_* columns, module values inherited
    line: int = 0
    route: str = ''     # '' is the tech.yaml route
    spx_kind: str = 'as_csv'        # as_csv, custom_noop or zero_filled (spxsteps.classify)
    zero_filled: dict = field(default_factory=dict)   # spx parameters set to 0, decoded

    @property
    def stem(self):
        return f'{self.index:04d}_{slug(self.module, 24)}_{slug(self.name)}'

    @property
    def comment(self):
        return self.cells.get('Comment', '')

    def value(self, col):
        return resolve(self.params[col]) if col in self.params else None

    @property
    def spx_params(self):
        """(step parameters, signature parameters): signature.* cells go to a separate signature call."""
        n = len(SIGNATURE_PREFIX)
        step = {k: v for k, v in self.params.items() if not k.startswith(SIGNATURE_PREFIX)}
        return step, {k[n:]: v for k, v in self.params.items() if k.startswith(SIGNATURE_PREFIX)}


@dataclass
class Flow:
    csv_path: str
    modules: list = field(default_factory=list)
    steps: list = field(default_factory=list)

    @property
    def routes(self):
        """Routes in the order they first appear ('' is the tech.yaml route)."""
        return list(dict.fromkeys(m.route for m in self.modules))

    @property
    def flows(self):
        """(route, flow) pairs in module order."""
        return list(dict.fromkeys((m.route, m.flow) for m in self.modules))

    def truncate(self, n):
        """Keep the first n steps (and the modules they use)."""
        self.steps = self.steps[:n]
        used = {(s.route, s.flow, s.module) for s in self.steps}
        self.modules = [m for m in self.modules if (m.route, m.flow, m.name) in used]
        return self


def _split(row, where):
    params, cells, carbon = {}, {}, {}
    for col, text in row.items():
        if col in FIXED_COLS or col == ROUTE_COL or col is None or text is None or text.strip() == '':
            continue
        text = text.strip()
        if col.startswith(CARBON_PREFIX):
            carbon[col] = text
            continue
        try:
            params[col] = decode(col, text)
        except ValueError as e:
            raise ValueError(f'{where}: {col}: {e}')
        cells[col] = text
    return params, cells, carbon


_warned = set()


def _warn(msg):
    """Print a CSV warning once per run (several stages load the same CSV)."""
    if msg not in _warned:
        _warned.add(msg)
        print(f'WARNING: {msg}', file=sys.stderr)


def load_flow(csv_path):
    with open(csv_path, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    for n, r in enumerate(rows, 2):
        r['_line'] = n
        try:
            r['_seq'] = float(r['seq'])
        except ValueError:
            raise ValueError(f'{csv_path}:{n}: seq {r["seq"]!r} is not a number')
    rows.sort(key=lambda r: r['_seq'])

    flow = Flow(str(csv_path))
    by_module, pending, names, seqs = {}, [], defaultdict(set), {}
    for r in rows:
        line, seq = r.pop('_line'), r.pop('_seq')
        where = f'{csv_path}:{line}'
        if seq in seqs:
            _warn(f'{where}: seq {r["seq"]} is also on line {seqs[seq]}; they run in CSV order')
        seqs.setdefault(seq, line)
        kind = r['row_type'].strip()
        params, cells, carbon = _split(r, where)
        route = (r.get(ROUTE_COL) or '').strip()
        key = (route, r['flow'], r['module'])
        if kind == 'module':
            if key in by_module:
                raise ValueError(f'{where}: module {r["module"]!r} defined twice')
            by_module[key] = Module(r['flow'], r['module'], params, carbon, route)
            flow.modules.append(by_module[key])
        elif kind == 'step':
            name = (r['step'] or '').strip()
            if not r['step_type'] or not name:
                raise ValueError(f'{where}: step rows need step and step_type')
            taken = names[key]
            if name in taken or RESERVED_STEP_RE.fullmatch(name):
                new = next(f'{name}_{n}' for n in itertools.count(2) if f'{name}_{n}' not in taken)
                why = (f'is already a step of module {r["module"]!r}' if name in taken
                       else 'is reserved for the steps CarbonEDA adds')
                _warn(f'{where}: step name {name!r} {why}; spx needs distinct step names in a module, '
                      f'so it runs as {new!r} (rename it in the CSV)')
                name = new
            taken.add(name)
            pending.append(Step(0, seq, r['flow'], r['module'], name, r['step_type'].strip(),
                                params, cells, carbon, line, route))
        else:
            raise ValueError(f'{where}: row_type must be "module" or "step", got {kind!r}')

    order = {key: i for i, key in enumerate(by_module)}
    for s in pending:
        if (s.route, s.flow, s.module) not in order:
            raise ValueError(f'{csv_path}:{s.line}: step {s.name!r} uses module {s.module!r} with no module row'
                             + (f' in route {s.route!r}' if s.route else ''))
    # spx runs each route in full before the next, its modules in definition order and each
    # module's steps in creation order.
    rank = {route: i for i, route in enumerate(dict.fromkeys(key[0] for key in by_module))}
    pending.sort(key=lambda s: (rank[s.route], order[(s.route, s.flow, s.module)], s.seq))
    for i, s in enumerate(pending, 1):
        s.index = i
        s.carbon = {**by_module[(s.route, s.flow, s.module)].carbon, **s.carbon}
    flow.steps = pending
    return flow


def columns(params, route=False):
    """The standard column order: FIXED_COLS (with route after row_type when the CSV uses it),
    CARBON_COLS, then the parameters, PREFERRED_COLS first and the rest alphabetically."""
    fixed = FIXED_COLS[:2] + [ROUTE_COL] + FIXED_COLS[2:] if route else FIXED_COLS
    return fixed + CARBON_COLS + [c for c in PREFERRED_COLS if c in params] + \
        sorted(set(params) - set(PREFERRED_COLS))


def _flow_key(flow):
    """What spx and the carbon model get from a flow, with py: cells read as Python values and no
    Comment (tidy() joins its lines)."""
    def value(v):
        r = resolve(v)
        return v.text if r is None and isinstance(v, Raw) else r
    return ([(m.route, m.flow, m.name, m.carbon) for m in flow.modules],
            [(s.route, s.flow, s.module, s.name, s.step_type, s.carbon,
              {k: value(v) for k, v in s.params.items() if k != 'Comment'}) for s in flow.steps])


def tidy(path):
    """Rewrite a flow CSV in the standard layout (columns()): columns empty on every row are dropped
    (carboneda_process_area is kept), py:True/py:False become True/False, and a line break in a
    Comment becomes a space. Rows keep their order and line endings; raises if the flow would read
    back differently."""
    with open(path, newline='', encoding='utf-8') as f:
        head = f.readline()
        f.seek(0)
        rows = list(csv.DictReader(f))
    before = _flow_key(load_flow(path))
    params = set()
    for r in rows:
        for col in list(r):
            text = (r[col] or '').strip() if col is not None else ''
            if col is None or not text:
                r.pop(col)
                continue
            if text in ('py:True', 'py:False'):
                text = text[3:]
            if col == 'Comment':
                text = ' '.join(text.split())
            r[col] = text
            if col not in FIXED_COLS + CARBON_COLS + [ROUTE_COL]:
                params.add(col)
    for r in rows:
        r.setdefault('carboneda_process_area', '')
    cols = columns(params, any(r.get(ROUTE_COL) for r in rows))
    cols = [c for c in cols if c in FIXED_COLS or c == 'carboneda_process_area' or any(c in r for r in rows)]
    tmp = f'{path}.tidy'
    with open(tmp, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator='\r\n' if head.endswith('\r\n') else '\n')
        w.writeheader()
        w.writerows(rows)
    if _flow_key(load_flow(tmp)) != before:
        os.remove(tmp)
        raise ValueError(f'{path}: tidy would change the flow; left as it was')
    os.replace(tmp, path)
    return cols
