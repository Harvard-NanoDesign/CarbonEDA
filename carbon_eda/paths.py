# Author: David Kong
# License: MIT

"""Repo layout: technology inputs, shared config, and pipeline outputs."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / 'config'
TECH_DIR = ROOT / 'technologies'
OUTPUTS_DIR = ROOT / 'outputs'
TCL_DIR = Path(__file__).resolve().parent

LAYOUT_EXTS = ('.gds', '.gdsii', '.oas', '.oasis')


def rel(path):
    """path relative to the repo (as written in reports and the manifest), or as given when outside it."""
    path = Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)


def load_yaml(path):
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def tools_config():
    """config/tools.yaml, with config/tools.local.yaml (if present) merged over it."""
    cfg = load_yaml(CONFIG_DIR / 'tools.yaml')
    local = CONFIG_DIR / 'tools.local.yaml'
    if local.exists():
        for key, value in load_yaml(local).items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                for k, v in value.items():
                    cfg[key][k] = {**cfg[key][k], **v} if isinstance(v, dict) and isinstance(cfg[key].get(k), dict) else v
            else:
                cfg[key] = value
    return cfg


def tool(name, args=''):
    """Shell command running a Synopsys tool per config/tools.yaml: '<setup> && <command> <args>'."""
    t = tools_config().get('tools', {}).get(name)
    if not t or not t.get('command'):
        raise SystemExit(f'config/tools.yaml: no tools.{name}.command')
    run = f"{t['command']} {args}".strip()
    return f"{t['setup']} && {run}" if t.get('setup') else run


def all_techs():
    """Every technology (a folder with a tech.yaml) as its path under technologies/, e.g. 'logic/ff'."""
    return sorted(p.parent.relative_to(TECH_DIR).as_posix() for p in TECH_DIR.rglob('tech.yaml'))


def _resolve(spec):
    """'logic/ff' as given; a bare 'ff' is looked up in every category subfolder."""
    spec = spec.strip('/')
    if '/' in spec or (TECH_DIR / spec).is_dir():
        return spec
    found = [t for t in all_techs() if t.rsplit('/', 1)[-1] == spec]
    if len(found) > 1:
        raise SystemExit(f'technology {spec!r} is ambiguous: {", ".join(found)}; give the full path')
    return found[0] if found else spec


class Tech:
    """One technology: inputs under technologies/[<category>/]<name>/, outputs under
    outputs/[<category>/]<name>/ (categories such as dram, flash, logic, photonics)."""

    def __init__(self, spec):
        self.path = _resolve(spec)
        self.name = self.path.rsplit('/', 1)[-1]
        self.src = TECH_DIR / self.path
        self.tech_yaml = self.src / 'tech.yaml'
        self.outputs = OUTPUTS_DIR / self.path
        self.spx_dir = self.outputs / 'spx'
        self.tdr_dir = self.outputs / 'tdr'
        self.tbson_dir = self.outputs / 'tbson'
        self.img_dir = self.outputs / 'img'
        self.carbon_dir = self.outputs / 'carbon'
        self.report_dir = self.outputs / 'report'
        self.manifest = self.outputs / 'manifest.json'

    @property
    def cfg(self):
        return load_yaml(self.tech_yaml) if self.tech_yaml.exists() else {}

    @property
    def csv(self):
        """The flow CSV that is run: csv: in tech.yaml (other CSVs in the folder are not read)."""
        given = self.cfg.get('csv')
        if not given:
            found = sorted(p.name for p in self.src.glob('*.csv')) if self.src.is_dir() else []
            raise SystemExit(f'{self.tech_yaml}: set "csv:" to the flow CSV to run'
                             + (f' (CSVs here: {", ".join(found)})' if found else ''))
        return self.src / given

    @property
    def routes(self):
        """The routes spx runs, in order (tech.yaml routes:, or route: for just one). The last is the
        flow CSV; any before it are carrier wafers its bond_structure steps bond onto, each
        {route, flow, module, step, substrate}."""
        routes = self.cfg.get('routes') or [{'route': self.cfg.get('route', self.name)}]
        for r in routes[:-1]:
            missing = [k for k in ('route', 'flow', 'module', 'step') if not r.get(k)]
            if missing:
                raise SystemExit(f'{self.tech_yaml}: routes entry {r.get("route", "?")!r} needs {", ".join(missing)}')
        return routes

    @property
    def route(self):
        return self.routes[-1]['route']

    @property
    def layout(self):
        given = self.cfg.get('layout')
        if given:
            path = self.src / given
            if not path.is_file():
                raise SystemExit(f'{self.tech_yaml}: layout {given!r} not found')
            return path
        found = sorted(p for p in self.src.iterdir() if p.suffix.lower() in LAYOUT_EXTS)
        if len(found) != 1:
            raise SystemExit(f'{self.src}: expected one layout file, found {[p.name for p in found] or "none"}; '
                             'set "layout:" in tech.yaml')
        return found[0]

    def require(self):
        if not self.tech_yaml.is_file():
            raise SystemExit(f'{self.tech_yaml} not found (technologies: {all_techs()})')
        if not self.csv.is_file():
            raise SystemExit(f'{self.tech_yaml}: csv {self.csv.name!r} not found in {self.src}')
        return self
