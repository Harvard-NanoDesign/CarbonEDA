# Author: David Kong
# License: MIT

"""GPW and gas MPW: process gases for each step, per wafer.

A step uses the input gases carbon_config.yaml input_gases_per_step lists under
its process area, kg_gas of each per step.

  GPW      direct emissions after abatement (carbon_config.yaml gpw:), for each input gas:
             unused     kg_gas x (1 - utilization) x (1 - abatement) x GWP
             byproduct  kg_gas x kg per kg gas x (1 - abatement) x GWP, for
                        each byproduct the gas forms in that process area
           utilization and abatement_rate fall back to gpw: defaults.
  gas MPW  upstream production of the input gases: kg_gas x kgCO2e/kg.
"""

from collections import defaultdict

AREA_KEYS_RESERVED = ('defaults', 'gases')


class Gpw:
    def __init__(self, cfg):
        g = cfg.gpw
        self.upstream = [{'gas': gas, 'area': area, 'kg': float(u['kg_gas']), 'gwp': float(u['kgco2e_per_kg_gas'])}
                         for area, gases in (cfg.carbon.get('input_gases_per_step') or {}).items()
                         for gas, u in (gases or {}).items()]
        defaults = g.get('defaults') or {}
        self.default_utilization = float(defaults.get('utilization', 1.0))
        self.default_abatement = float(defaults.get('abatement_rate', 0.0))
        gases = g.get('gases') or {}
        self.gwp = {gas: float(v['gwp_kgco2e_per_kg']) for gas, v in gases.items()}
        self.abatement = {gas: float(v['abatement_rate']) for gas, v in gases.items() if 'abatement_rate' in v}
        self.areas = {area: gases or {} for area, gases in g.items() if area not in AREA_KEYS_RESERVED}
        used = {u['gas'] for u in self.upstream}
        for gases in self.areas.values():
            for gas, f in gases.items():
                used |= {gas} | set((f or {}).get('byproducts') or {})
        missing = sorted(used - set(self.gwp))
        if missing:
            raise ValueError('carbon_config.yaml: no gwp_kgco2e_per_kg under gpw: gases: for ' + ', '.join(missing))

    def _unabated(self, gas):
        """Fraction of an exhausted `gas` that the abatement system lets through."""
        return 1.0 - self.abatement.get(gas, self.default_abatement)

    def _emitted(self, area):
        """[kg emitted, kgCO2e] per step of each (gas, role) on a step in process area `area`."""
        out = defaultdict(lambda: [0.0, 0.0])
        for u in self.upstream:
            if u['area'] != area:
                continue
            f = self.areas.get(area, {}).get(u['gas']) or {}
            utilization = float(f.get('utilization', self.default_utilization))
            exhausted = [(u['gas'], 'unused', u['kg'] * (1.0 - utilization))]
            exhausted += [(b, 'byproduct', u['kg'] * float(kg_per_kg))
                          for b, kg_per_kg in (f.get('byproducts') or {}).items()]
            for gas, role, kg in exhausted:
                kg *= self._unabated(gas)
                out[(gas, role)][0] += kg
                out[(gas, role)][1] += kg * self.gwp[gas]
        return out

    def step(self, area):
        """kgCO2e totals and per-gas rows (gCO2e) for one step in process area `area`."""
        rows, mpw_kg, gpw_kg = [], 0.0, 0.0
        for (gas, role), (kg, kgco2e) in self._emitted(area).items():
            if kg:
                gpw_kg += kgco2e
                rows.append({'gas': gas, 'role': role, 'supplied_g': 0.0, 'emitted_g': kg * 1000.0,
                             'mpw_gco2e': 0.0, 'gpw_gco2e': kgco2e * 1000.0})
        for u in self.upstream:
            if u['area'] == area:
                kg = u['kg'] * u['gwp']
                mpw_kg += kg
                rows.append({'gas': u['gas'], 'role': 'upstream', 'supplied_g': u['kg'] * 1000.0, 'emitted_g': 0.0,
                             'mpw_gco2e': kg * 1000.0, 'gpw_gco2e': 0.0})
        return {'mpw_gas_kgco2e': mpw_kg, 'gpw_kgco2e': gpw_kg, 'gases': rows}
