# Author: David Kong
# License: MIT

"""Stage 1b: EPW, MPW and GPW for every step of the flow CSV, and for each carrier wafer.

outputs/<tech>/carbon/
  steps.csv      one row per step (keyed by index, as in manifest.json); first, one row per carrier
                 wafer (tech.yaml routes: before the flow), index 0, charged its wafer
  gases.csv      per-step gas detail behind the GPW and gas MPW numbers
  by_module.csv  by_area.csv  totals.csv
                 step counts leave out uncounted_process_areas (config/carbon_config.yaml: Wafer)
                 and weight each step by process_area_step_count
All carbon values are kgCO2e per wafer start unless the column says otherwise.
"""

import csv
import math
from collections import defaultdict

from .. import csvio
from .config import Config
from .epw import Epw, overhead_fraction
from .gpw import Gpw
from .mpw import Mpw

# EPW is split into tool and facility overhead (energy_kwh = tool + overhead, epw = tool_epw + overhead_epw).
EPW_FIELDS = ['tool_energy_kwh', 'overhead_energy_kwh', 'energy_kwh',
              'tool_epw_kgco2e', 'overhead_epw_kgco2e', 'epw_kgco2e']
STEP_FIELDS = ['index', 'seq', 'module', 'step', 'step_type', 'process_area'] + EPW_FIELDS + \
              ['thickness_um', 'materials', 'mass_g', 'mpw_material_kgco2e', 'mpw_gas_kgco2e', 'mpw_kgco2e',
               'gpw_kgco2e', 'total_kgco2e', 'notes']
GAS_FIELDS = ['index', 'gas', 'role', 'supplied_g', 'emitted_g', 'mpw_gco2e', 'gpw_gco2e']
SUM_FIELDS = EPW_FIELDS + ['mpw_kgco2e', 'gpw_kgco2e', 'total_kgco2e']
CARRIER_MODULE = 'Carrier wafer'


def carrier_steps(carriers):
    """The carrier wafers (tech.yaml routes: before the flow, each {route, flow, module, step, substrate}) as
    substrate steps, so each is charged its wafer. They are not flow CSV steps: index 0 (flow steps count from 1)."""
    return [csvio.Step(index=0, seq=0.0, flow=c['flow'], module=CARRIER_MODULE, name=c['route'], step_type='substrate',
                       params={}, cells={}, carbon={'carboneda_process_area': 'Wafer'}, route=c['route'])
            for c in carriers]


def step_rows(flow, cfg, carriers=()):
    epw, mpw, gpw = Epw(cfg), Mpw(cfg), Gpw(cfg)
    rows, gases = [], []
    for s in carrier_steps(carriers) + flow.steps:
        e = epw.step(s)
        m = mpw.step(s)
        g = gpw.step(e['process_area'])
        notes = []
        if e['note']:
            notes.append(e['note'])
        if m['unmapped']:
            notes.append('no MPW data for ' + '|'.join(m['unmapped']))
        mpw_mat = sum(x[2] for x in m['materials']) / 1000.0
        mpw_gas = g['mpw_gas_kgco2e']
        row = {
            'index': s.index, 'seq': s.seq, 'module': s.module, 'step': s.name, 'step_type': s.step_type,
            'process_area': e['process_area'],
            **{f: e[f] for f in EPW_FIELDS},
            'thickness_um': m['thickness_um'] if m['thickness_um'] is not None else '',
            'materials': '|'.join(x[0] for x in m['materials']),
            'mass_g': sum(x[1] for x in m['materials']),
            'mpw_material_kgco2e': mpw_mat, 'mpw_gas_kgco2e': mpw_gas, 'mpw_kgco2e': mpw_mat + mpw_gas,
            'gpw_kgco2e': g['gpw_kgco2e'],
            'notes': '; '.join(notes),
        }
        row['total_kgco2e'] = row['epw_kgco2e'] + row['mpw_kgco2e'] + row['gpw_kgco2e']
        rows.append(row)
        gases += [dict(gas, index=s.index) for gas in g['gases']]
    return rows, gases


def uncounted(cfg):
    """Process areas left out of the step counts (uncounted_process_areas in config/carbon_config.yaml)."""
    return set(cfg.carbon.get('uncounted_process_areas') or ())


def step_count(cfg):
    """Steps a step of each process area counts as: process_area_step_count in config/carbon_config.yaml
    (default 1), 0 for uncounted_process_areas. Sums of them are rounded up to whole steps."""
    counts = dict(cfg.carbon.get('process_area_step_count') or {})
    default = counts.pop('default', 1)
    skip = uncounted(cfg)
    return lambda area: 0 if area in skip else counts.get(area, default)


def group(rows, key, count):
    out = defaultdict(lambda: dict.fromkeys(['steps'] + SUM_FIELDS, 0))
    for r in rows:
        agg = out[r[key]]
        agg['steps'] += count(r['process_area'])
        for f in SUM_FIELDS:
            agg[f] += r[f]
    return [{key: k, **v, 'steps': math.ceil(v['steps'])} for k, v in out.items()]


def totals(rows, cfg, die_area_mm2=None):
    fab = cfg.fab
    t = {f: sum(r[f] for r in rows) for f in SUM_FIELDS}
    for f in ('mpw_material_kgco2e', 'mpw_gas_kgco2e'):
        t[f] = sum(r[f] for r in rows)
    skip, count = uncounted(cfg), step_count(cfg)
    out = {'steps': math.ceil(sum(count(r['process_area']) for r in rows if r['index'] > 0)),
           'uncounted_steps': sum(r['index'] > 0 and r['process_area'] in skip for r in rows),
           'carrier_wafers': sum(r['index'] == 0 for r in rows),
           'wafer_diameter_mm': fab.wafer_diameter_mm, 'yield': fab.yield_,
           'ci_fab_kgco2e_per_kwh': float(cfg.carbon['ci_fab_kgco2e_per_kwh']),
           'facility_overhead_fraction': overhead_fraction(cfg.carbon), **t,
           'total_kgco2e_per_good_wafer': t['total_kgco2e'] / fab.yield_}
    if die_area_mm2:
        dies = fab.dies_per_wafer(float(die_area_mm2))
        out.update({'die_area_mm2': float(die_area_mm2), 'gross_dies_per_wafer': dies,
                    'total_kgco2e_per_good_die': t['total_kgco2e'] / (dies * fab.yield_)})
    return out


def _write(path, rows, fields):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def run(tech, flow, cfg=None):
    cfg = cfg or Config()
    rows, gases = step_rows(flow, cfg, tech.routes[:-1])
    tot = totals(rows, cfg, tech.cfg.get('die_area_mm2'))
    d = tech.carbon_dir
    d.mkdir(parents=True, exist_ok=True)
    _write(d / 'steps.csv', rows, STEP_FIELDS)
    _write(d / 'gases.csv', gases, GAS_FIELDS)
    _write(d / 'by_module.csv', group(rows, 'module', step_count(cfg)), ['module', 'steps'] + SUM_FIELDS)
    _write(d / 'by_area.csv', group(rows, 'process_area', step_count(cfg)), ['process_area', 'steps'] + SUM_FIELDS)
    _write(d / 'totals.csv', [{'metric': k, 'value': v} for k, v in tot.items()], ['metric', 'value'])
    flagged = sum(1 for r in rows if r['notes'])
    print(f'{d}: EPW {tot["epw_kgco2e"]:.2f} (tool {tot["tool_epw_kgco2e"]:.2f} + facility overhead '
          f'{tot["overhead_epw_kgco2e"]:.2f})  MPW {tot["mpw_kgco2e"]:.2f}  GPW {tot["gpw_kgco2e"]:.2f}  '
          f'total {tot["total_kgco2e"]:.2f} kgCO2e/wafer'
          + (f'  (incl. {tot["carrier_wafers"]} carrier wafer{"s" if tot["carrier_wafers"] > 1 else ""})'
             if tot['carrier_wafers'] else '')
          + (f'  ({flagged} steps with notes, see steps.csv)' if flagged else ''))
    return rows, tot
