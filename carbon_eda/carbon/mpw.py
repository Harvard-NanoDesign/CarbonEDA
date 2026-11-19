# Author: David Kong
# License: MIT

"""MPW: embodied carbon of the material each step deposits, per wafer.

A depo_* or interface_growth step deposits its thickness (Thickness, Offset,
Rate x Time, or for depo_epi Rate100 x Time) of each MaterialFilter material as
a blanket film over the whole wafer; mass = wafer area x thickness x density
and MPW = mass x gwp_factor (materials.yaml). The substrate step is charged the wafer
itself when materials.yaml has a per-300mm-wafer entry for it. Upstream input-gas
MPW (carbon_config.yaml input_gases_per_step) is computed per process area in gpw.py.
"""

import re

from .config import norm

LENGTH_UM = {'um': 1.0, 'nm': 1e-3, 'A': 1e-4, 'Angstrom': 1e-4, 'mm': 1e3, 'cm': 1e4, 'm': 1e6}
TIME_MIN = {'min': 1.0, 's': 1 / 60, 'sec': 1 / 60, 'h': 60.0, 'hr': 60.0}
RATE_RE = re.compile(r'^\s*(\w+)\s*(?:/\s*(\w+)|\s+(\w+)\^-1)\s*$')
EPI_RATES = ('Rate100', 'Rate110', 'Rate111', 'Rate311')


def _qty(v):
    if isinstance(v, tuple) and len(v) == 2 and hasattr(v[1], 'name') and isinstance(v[0], (int, float)):
        return float(v[0]), v[1].name
    return None


def length_um(v):
    q = _qty(v)
    return q[0] * LENGTH_UM[q[1]] if q and q[1] in LENGTH_UM else None


def time_min(v):
    q = _qty(v)
    return q[0] * TIME_MIN[q[1]] if q and q[1] in TIME_MIN else None


def rate_um_per_min(v):
    q = _qty(v)
    m = RATE_RE.match(q[1]) if q else None
    if not m:
        return None
    length, time = m.group(1), m.group(2) or m.group(3)
    if length not in LENGTH_UM or time not in TIME_MIN:
        return None
    return q[0] * LENGTH_UM[length] / TIME_MIN[time]


def thickness_um(step):
    for col in ('Thickness', 'Offset'):
        t = length_um(step.value(col))
        if t is not None:
            return t
    t = time_min(step.value('Time'))
    if t is None:
        return None
    rate = rate_um_per_min(step.value('Rate'))
    if rate is not None:
        return rate * t
    if step.step_type == 'depo_epi':
        rates = {c: rate_um_per_min(step.value(c)) for c in EPI_RATES}
        if rates['Rate100'] is not None:
            return rates['Rate100'] * t
        known = [r for r in rates.values() if r is not None]
        return sum(known) / len(known) * t if known else None
    if step.step_type == 'interface_growth':
        for col in sorted(step.params):
            rate = rate_um_per_min(step.value(col))
            if rate is not None:
                return rate * t
    return None


def step_materials(value):
    """Every material named in a ('Material'|'Name', 'equals', X) clause anywhere in a parameter value."""
    if isinstance(value, tuple) and len(value) == 3 and value[0] in ('Material', 'Name') and value[1] == 'equals':
        return [value[2]]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        return [m for v in value for m in step_materials(v)]
    return []


def filter_materials(value):
    """Material names in a MaterialFilter value: [[('Material'|'Name', 'equals', X)], ...]."""
    names = []
    for group in value or []:
        for clause in group if isinstance(group, list) else [group]:
            if isinstance(clause, tuple) and len(clause) == 3 and clause[1] == 'equals':
                names.append(clause[2])
    return names


class Mpw:
    def __init__(self, cfg):
        self.cfg = cfg
        self.props, self.alias = {}, {}
        for canonical, p in cfg.materials.items():
            if 'mpw_gco2e_per_300mm_wafer' in p:
                self.props[canonical] = {'wafer_gco2e_300mm': float(p['mpw_gco2e_per_300mm_wafer'])}
            elif 'density_g_per_cm3' in p:
                self.props[canonical] = {'density': float(p['density_g_per_cm3']),
                                         'gwp': float(p['gwp_factor_gco2e_per_g'])}
            for name in [canonical] + list(p.get('aliases', []) or []) + list(p.get('spx_colors') or {}):
                self.alias[norm(name)] = canonical

    def canonical(self, name):
        return self.alias.get(norm(name))

    def has_data(self, name):
        """True when materials.yaml has carbon data (density and GWP, or per wafer) for `name`."""
        return self.canonical(name) in self.props

    def step(self, step):
        """{'thickness_um', 'materials': [(canonical, mass_g, mpw_g)], 'unmapped': [names]}"""
        area_cm2 = self.cfg.fab.wafer_area_cm2
        out = {'thickness_um': None, 'materials': [], 'unmapped': []}
        if step.step_type == 'substrate':
            wafer = self.canonical('Wafer')
            if wafer and 'wafer_gco2e_300mm' in self.props[wafer]:
                scale = self.cfg.fab.wafer_diameter_mm ** 2 / 300.0 ** 2
                out['materials'].append((wafer, 0.0, self.props[wafer]['wafer_gco2e_300mm'] * scale))
            return out
        if not (step.step_type.startswith('depo_') or step.step_type == 'interface_growth'):
            return out
        t = thickness_um(step)
        out['thickness_um'] = t
        if not t or t <= 0:
            return out
        for name in filter_materials(step.value('MaterialFilter')):
            canonical = self.canonical(name)
            p = self.props.get(canonical)
            if not p or 'density' not in p:
                out['unmapped'].append(name)
                continue
            mass_g = area_cm2 * t * 1e-4 * p['density']
            out['materials'].append((canonical, mass_g, mass_g * p['gwp']))
        return out
