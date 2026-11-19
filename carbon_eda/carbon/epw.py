# Author: David Kong
# License: MIT

"""EPW: process energy per wafer for each step (config/carbon_config.yaml).

A step's process area is its carboneda_process_area, a key of energy_per_step_kwh in
carbon_config.yaml (Deposition, Dry_etch, ...; for a lithography step, the
litho tool such as EUV or ArF-i). Its tool energy is carboneda_energy_kwh if set, else
energy_per_step_kwh[area]. The substrate (Wafer) step has no process energy.
EPW is reported in two parts for audit, which add up to it:
  tool      tool energy x ci_fab_kgco2e_per_kwh
  overhead  tool energy x facility_overhead_fraction x ci_fab_kgco2e_per_kwh
"""


def overhead_fraction(carbon):
    """facility_overhead_fraction of carbon_config.yaml: facility energy per kWh of tool energy."""
    if 'facility_overhead' in carbon:
        raise ValueError('carbon_config.yaml: facility_overhead (a multiplier such as 1.4) is replaced by '
                         'facility_overhead_fraction, the overhead added (0.4 for the same fab)')
    return float(carbon.get('facility_overhead_fraction', 0.0))


class Epw:
    def __init__(self, cfg):
        c = cfg.carbon
        self.energy = {k: float(v) for k, v in c['energy_per_step_kwh'].items()}
        self.ci = float(c['ci_fab_kgco2e_per_kwh'])
        self.overhead = overhead_fraction(c)

    def tool_energy_kwh(self, step, area):
        """(kWh, note); kWh is None when carbon_config.yaml has no value for the step."""
        if step.carbon.get('carboneda_energy_kwh'):
            return float(step.carbon['carboneda_energy_kwh']), ''
        if not area:
            return None, 'no carboneda_process_area'
        if area == 'Wafer':
            return 0.0, ''
        if area in self.energy:
            return self.energy[area], ''
        return None, f'no energy for {area} in carbon_config.yaml'

    def step(self, step):
        area = step.carbon.get('carboneda_process_area')
        kwh, note = self.tool_energy_kwh(step, area)
        kwh = kwh or 0.0
        overhead_kwh = kwh * self.overhead
        return {
            'process_area': area or 'UNMAPPED',
            'tool_energy_kwh': kwh,
            'overhead_energy_kwh': overhead_kwh,
            'energy_kwh': kwh + overhead_kwh,
            'tool_epw_kgco2e': kwh * self.ci,
            'overhead_epw_kgco2e': overhead_kwh * self.ci,
            'epw_kgco2e': (kwh + overhead_kwh) * self.ci,
            'note': note,
        }
