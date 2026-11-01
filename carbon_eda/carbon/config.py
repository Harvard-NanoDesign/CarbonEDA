# Author: David Kong
# License: MIT

"""Loads the shared carbon assumptions in config/."""

import math
import re
from dataclasses import dataclass

from ..paths import CONFIG_DIR, load_yaml


def norm(name):
    return re.sub(r'[^A-Za-z0-9]+', '', str(name)).lower()


@dataclass
class Fab:
    wafer_diameter_mm: float
    yield_: float

    @property
    def wafer_area_cm2(self):
        return math.pi * (self.wafer_diameter_mm / 20.0) ** 2

    def dies_per_wafer(self, die_area_mm2):
        """Gross dies per wafer (standard edge-loss approximation)."""
        d = self.wafer_diameter_mm
        return math.pi * d * d / (4 * die_area_mm2) - math.pi * d / math.sqrt(2 * die_area_mm2)


class Config:
    def __init__(self, config_dir=CONFIG_DIR):
        y = lambda name: load_yaml(config_dir / name)
        self.materials = y('materials.yaml')
        self.fab = Fab(float(self.materials.pop('wafer_diameter_mm')), float(self.materials.pop('yield')))
        if not 0 < self.fab.yield_ <= 1:
            raise ValueError('materials.yaml: yield must be in (0, 1]')
        self.spx_colors = {}
        for entry in self.materials.values():
            for name, color in (entry.get('spx_colors') or {}).items():
                if name in self.spx_colors:
                    raise ValueError(f'materials.yaml: spx material {name!r} is listed twice')
                self.spx_colors[name] = color
        self.carbon = y('carbon_config.yaml')
        self.gpw = self.carbon.get('gpw') or {}
