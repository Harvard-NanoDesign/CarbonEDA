# Author: David Kong
# License: MIT

"""Checks for user errors in the flow CSVs and config/, plus the CSV codec and tdr compression.

    make test                            every technology
    CARBON_EDA_TECH=bond_flip make test   one technology (make all sets this to its TECH)
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from pathlib import Path

import yaml

from carbon_eda import csvio, generate, spxsteps, tdrzst
from carbon_eda.carbon import Config, step_rows
from carbon_eda.carbon.epw import overhead_fraction
from carbon_eda.layout import Layout, layer_names, layout_format
from carbon_eda.carbon.mpw import Mpw, filter_materials, step_materials
from carbon_eda.paths import Tech, all_techs

ONLY = os.environ.get('CARBON_EDA_TECH')


def _load_errors(techs):
    """{tech: why its tech.yaml or flow CSV doesn't load}; test_techs_load fails on these, and the
    other tests check the remaining technologies."""
    out = {}
    for t in techs:
        try:
            csvio.load_flow(Tech(t).require().csv)
        except (SystemExit, ValueError, KeyError) as e:
            out[t] = str(e)
    return out


SELECTED = [t for t in all_techs()
            if Tech(t).tech_yaml.exists() and (not ONLY or Tech(t).name == Tech(ONLY).name)]
LOAD_ERRORS = _load_errors(SELECTED)
TECHS = [t for t in SELECTED if t not in LOAD_ERRORS]


class Pipeline(unittest.TestCase):
    def test_techs_load(self):
        """Every technology's tech.yaml and flow CSV load (the other tests skip the ones that don't)."""
        self.assertFalse(LOAD_ERRORS, '\n' + '\n'.join(f'{t}: {e}' for t, e in LOAD_ERRORS.items()))

    def test_csv_codec(self):
        for col, text, v in [
                ('MaterialFilter', 'Name=Oxide|SiN', [[('Name', 'equals', 'Oxide')], [('Name', 'equals', 'SiN')]]),
                ('MaterialFilter', 'Oxide', [[('Material', 'equals', 'Oxide')]]),
                ('MaterialDependent', 'Name=Photoresist => 0.001 um min^-1',
                 [([[('Name', 'equals', 'Photoresist')]], (0.001, csvio.Unit('um min^-1')))]),
                ('Time', '8.5 min', (8.5, csvio.Unit('min'))), ('LayerNames', '401:0|402:0', ['401:0', '402:0']),
                ('Flip', 'True', True), ('ExposedOnly', 'False', False), ('ReferenceMaterialsFilter', '*', [[]]),
                ('MaterialDependent', '* => 0.5 um min^-1', [([[]], (0.5, csvio.Unit('um min^-1')))]),
                ('Flip', 'py:True', csvio.Raw('True'))]:
            self.assertEqual(csvio.decode(col, text), v)

    def test_carbon_runs_on_every_step(self):
        cfg = Config()
        for tech_name in TECHS:
            with self.subTest(tech=tech_name):
                flow = csvio.load_flow(Tech(tech_name).require().csv)
                rows, _ = step_rows(flow, cfg)
                self.assertEqual(len(rows), len(flow.steps))
                self.assertTrue(all(r['total_kgco2e'] >= 0 for r in rows))

    def test_process_areas_are_known(self):
        """Warns (does not fail) about steps whose carboneda_process_area is not an energy_per_step_kwh key or
        an uncounted area: a blank or renamed area gets no energy."""
        cfg = Config()
        known = set(cfg.carbon['energy_per_step_kwh']) | set(cfg.carbon.get('uncounted_process_areas') or ())
        for tech_name in TECHS:
            with self.subTest(tech=tech_name):
                bad = defaultdict(int)
                for r in step_rows(csvio.load_flow(Tech(tech_name).require().csv), cfg)[0]:
                    if r['process_area'] not in known:
                        bad[r['process_area'] or '(blank)'] += 1
                for area, n in sorted(bad.items()):
                    print(f'WARNING: {tech_name}: {n} steps have process area {area}, not in '
                          'config/carbon_config.yaml (no energy)', file=sys.stderr)

    def test_carrier_wafers_are_charged(self):
        """Each carrier wafer in tech.yaml routes: is one index-0 row charged the same wafer as the substrate step."""
        cfg = Config()
        for tech_name in TECHS:
            with self.subTest(tech=tech_name):
                tech = Tech(tech_name).require()
                flow = csvio.load_flow(tech.csv)
                rows, _ = step_rows(flow, cfg, tech.routes[:-1])
                carriers = [r for r in rows if r['index'] == 0]
                self.assertEqual(len(carriers), len(tech.routes) - 1, tech_name)
                wafer = next(r['mpw_kgco2e'] for r in rows if r['index'] > 0 and r['step_type'] == 'substrate')
                for r in carriers:
                    self.assertAlmostEqual(r['mpw_kgco2e'], wafer, msg=tech_name)

    def test_epw_is_tool_plus_overhead(self):
        cfg = Config()
        overhead = overhead_fraction(cfg.carbon)
        for tech_name in TECHS:
            with self.subTest(tech=tech_name):
                for r in step_rows(csvio.load_flow(Tech(tech_name).require().csv), cfg)[0]:
                    self.assertAlmostEqual(r['tool_epw_kgco2e'] + r['overhead_epw_kgco2e'], r['epw_kgco2e'])
                    self.assertAlmostEqual(r['tool_energy_kwh'] * overhead, r['overhead_energy_kwh'])
                    self.assertAlmostEqual(r['tool_energy_kwh'] + r['overhead_energy_kwh'], r['energy_kwh'])

    def test_duplicate_step_names_are_renamed(self):
        """A repeated step name in a module, or one CarbonEDA adds itself, warns and runs under a distinct name."""
        text = ('seq,row_type,flow,module,step,step_type\n'
                '10,module,F,M,,\n20,step,F,M,etch,etch_isotropic\n30,step,F,M,etch,etch_isotropic\n'
                '40,step,F,M,etch_2,etch_isotropic\n50,step,F,M,save_tbson,etch_isotropic\n'
                '60,module,F,N,,\n70,step,F,N,etch,etch_isotropic\n70,step,F,N,depo,depo_isotropic\n')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'flow.csv'
            path.write_text(text)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                flow = csvio.load_flow(path)
            route = generate.route_py('R', flow, Path(tmp), Path(tmp) / 'R.tbson')
        self.assertEqual([s.name for s in flow.steps],
                         ['etch', 'etch_2', 'etch_2_2', 'save_tbson_2', 'etch', 'depo'])
        self.assertEqual(err.getvalue().count('WARNING'), 4, err.getvalue())   # 3 renames, 1 repeated seq
        locs = [line.split('Location=')[1].split('}')[0] for line in route.splitlines() if 'Location=' in line]
        self.assertEqual(len(locs), len(set(locs)), 'spx locations repeat in route.py')

    def test_bonded_routes_are_listed(self):
        """Every route a bond_structure step bonds onto is in tech.yaml routes: before the flow,
        or is a route of the flow CSV."""
        for tech_name in TECHS:
            with self.subTest(tech=tech_name):
                tech = Tech(tech_name).require()
                flow = csvio.load_flow(tech.csv)
                missing = generate.bond_routes(flow) - {r['route'] for r in tech.routes[:-1]} - set(flow.routes)
                self.assertFalse(missing, f'{tech_name}: add {sorted(missing)} to routes: in tech.yaml')

    def test_csv_routes(self):
        """A route column builds each wafer on its own route, in order, whatever the module order in the CSV;
        a BondTopStep naming a route bonds its last step."""
        text = ('seq,row_type,route,flow,module,step,step_type,BondTopStep\n'
                '10,module,TOP,FT,M,,,\n20,step,TOP,FT,M,sub,substrate,\n'
                '30,module,,FB,M,,,\n40,step,,FB,M,sub,substrate,\n50,step,,FB,M,bond,bond_structure,TOP\n'
                '60,module,TOP,FT,N,,,\n70,step,TOP,FT,N,cap,depo_isotropic,\n'
                '80,step,TOP,FT,N,save,meas_save_3D,\n')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'flow.csv'
            path.write_text(text)
            flow = csvio.load_flow(path)
            route = generate.route_py('R', flow, Path(tmp), Path(tmp) / 'R.tbson')
        self.assertEqual(flow.routes, ['TOP', ''])
        self.assertEqual([(s.route, s.name) for s in flow.steps],
                         [('TOP', 'sub'), ('TOP', 'cap'), ('TOP', 'save'), ('', 'sub'), ('', 'bond')])
        self.assertNotIn('route=', route)
        self.assertIn("{'Route': 'R', 'Flow': 'FB', 'Module': 'M', 'Step': 'bond'}", route)
        self.assertIn("Map_like_string_string({'Flow': 'FT', 'Module': 'N', 'Route': 'TOP', 'Step': 'cap'})", route)
        self.assertIn(f"{{'TOP': '{tmp}/TOP.tbson', 'R': '{tmp}/R.tbson'}}", route)
        self.assertEqual(generate.bond_routes(flow), {'TOP'})

    def test_carrier_bond_by_name(self):
        """A BondTopStep naming a carrier route (tech.yaml routes:) bonds its substrate step."""
        text = ('seq,row_type,flow,module,step,step_type,BondTopStep,Flip\n'
                '10,module,F,M,,,,\n20,step,F,M,sub,substrate,,\n30,step,F,M,bond,bond_structure,CAR,True\n')
        carrier = {'route': 'CAR', 'flow': 'CF', 'module': 'W', 'step': 'substrate'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'flow.csv'
            path.write_text(text)
            flow = csvio.load_flow(path)
            route = generate.route_py('R', flow, Path(tmp), Path(tmp) / 'R.tbson', [carrier])
        self.assertIn("BondTopStep=(parameter_reflection.description.Map_like_string_string("
                      "{'Flow': 'CF', 'Module': 'W', 'Route': 'CAR', 'Step': 'substrate'}),),Flip=True,", route)
        self.assertEqual(generate.bond_routes(flow), {'CAR'})

    def test_tidy(self):
        """fmt: the standard column order, empty columns dropped, py:True as True, one-line Comments;
        the flow reads back the same."""
        text = ('Time,seq,step,Flip,row_type,flow,module,step_type,Unused,carboneda_process_area,Comment\n'
                ',10,,,module,F,M,,,,"two\nlines"\n'
                '1.0 min,20,dep,,step,F,M,depo_isotropic,,CVD,\n'
                ',30,bond,py:True,step,F,M,bond_structure,,Wafer,\n')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'flow.csv'
            path.write_text(text)
            before = csvio.load_flow(path)
            csvio.tidy(path)
            after = csvio.load_flow(path)
            lines = path.read_text().splitlines()
        self.assertEqual(lines[0], 'seq,row_type,flow,module,step,step_type,carboneda_process_area,Comment,Flip,Time')
        self.assertEqual(lines[3], '30,step,F,M,bond,bond_structure,Wafer,,True,')
        self.assertEqual(after.modules[0].params['Comment'], 'two lines')
        self.assertEqual([(s.name, s.value('Flip'), s.value('Time')) for s in after.steps],
                         [(s.name, s.value('Flip'), s.value('Time')) for s in before.steps])

    def test_spx_step_kinds(self):
        """Carbon-only steps become no-op custom steps, built-in steps with no spx parameters get the
        zero_fill cells, and rows spx still can't run are errors with file:line."""
        text = ('seq,row_type,flow,module,step,step_type,Comment,Rate\n'
                '10,module,F,M,,,,\n30,step,F,M,wet,clean,it\'s wet,\n'
                '40,step,F,M,rta,anneal,RTA,\n50,step,F,M,dope,implant_ellipsoid,,\n'
                '60,step,F,M,flip,flip_structure,,\n70,step,F,M,etch,etch_isotropic,,0.1 um min^-1\n'
                '80,step,F,M,polish,clean,,\n')
        bad = ('seq,row_type,flow,module,step,step_type,Rate\n10,module,F,M,,,\n'
               '20,step,F,M,wet,clean,0.1 um min^-1\n30,step,F,M,etch,etch_isotropic,\n')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'flow.csv'
            path.write_text(text)
            flow = spxsteps.classify(csvio.load_flow(path))
            route = generate.route_py('R', flow, Path(tmp), Path(tmp) / 'R.tbson')
            path.write_text(bad)
            with self.assertRaises(ValueError) as e:
                spxsteps.classify(csvio.load_flow(path))
        self.assertEqual([s.spx_kind for s in flow.steps],
                         ['custom_noop', 'zero_filled', 'zero_filled', 'as_csv', 'as_csv', 'custom_noop'])
        self.assertEqual(sorted(flow.steps[1].zero_filled), ['TemperatureRamp'])
        self.assertIn(f'{path}:3', str(e.exception))   # clean with spx parameters
        self.assertIn(f'{path}:4', str(e.exception))   # etch_isotropic with none
        compile(route, 'route.py', 'exec')
        self.assertIn("'clean': 'def clean(**kwargs):\\n    return kwargs[\"ClipState\"]\\n'", route)
        self.assertEqual(route.count("spx.procedural.customstep(Location={'Route': 'R', 'Flow': 'F', 'Module': 'M', "
                                     "'Step': 'wet'}, Description='Carbon-accounting clean step: no geometry change',Name='clean',Parameters=[],"
                                     "Script=noop_step_scripts['clean'],StepType='process_step',)"), 1)
        self.assertIn("spx.procedural.modify(Target={'ProcessStep': {'Route': 'R', 'Flow': 'F', 'Module': 'M', "
                      "'Step': 'wet'}}, Comment=\"it's wet\",)", route)
        self.assertEqual(route.count('spx.procedural.modify('), 1)   # polish has no comment
        self.assertIn(f"Comment='RTA | {spxsteps.ZERO_NOTE}',", route)
        self.assertIn("TemperatureRamp=[((0.0, qupy.Unit('min'))", route)
        self.assertIn("Depth=(0.0, qupy.Unit('um')),", route)
        self.assertIn("MaterialFilter=[[('Material', 'equals', 'Silicon')]],", route)
        self.assertIn("spx.procedural.flip_structure(Location={'Route': 'R', 'Flow': 'F', 'Module': 'M', "
                      "'Step': 'flip'}, )", route)

    def test_zero_fill_needs_consent(self):
        """With no terminal to ask at, steps run with 0 values only after --zero-fill."""
        text = 'seq,row_type,flow,module,step,step_type\n10,module,F,M,,\n20,step,F,M,rta,anneal\n'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'flow.csv'
            path.write_text(text)
            flow = spxsteps.classify(csvio.load_flow(path))
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                spxsteps.confirm('T', flow, False, io.StringIO('y\n'))
            spxsteps.confirm('T', flow, True, io.StringIO())

    def test_spx_can_run_every_flow(self):
        """Every flow CSV's rows run in spx as written, as a no-op custom step or with 0 values
        (config/spx_steps.yaml); warns how many of the last two each has."""
        cfg = spxsteps.config()
        for tech_name in TECHS:
            with self.subTest(tech=tech_name):
                flow = spxsteps.classify(csvio.load_flow(Tech(tech_name).require().csv), cfg)
                line = spxsteps.summary(tech_name, flow)
                if line:
                    print(f'WARNING: {line}', file=sys.stderr)
        mismatch = spxsteps.version_mismatch()
        if mismatch:
            print(f'WARNING: {mismatch}', file=sys.stderr)

    def test_layouts_read(self):
        """Every technology's layout (GDSII or OASIS) reads, and each litho layer has shapes in the clip window."""
        try:
            import gdstk  # noqa: F401
        except ImportError:
            self.skipTest('gdstk not installed (make venv): the report has no mask callouts')
        for tech_name in TECHS:
            with self.subTest(tech=tech_name):
                tech = Tech(tech_name).require()
                layout = Layout(tech.layout, tech.cfg['clip']['coords'])
                self.assertEqual(layout.format, layout_format(tech.layout))
                layers = {ld for s in csvio.load_flow(tech.csv).steps if s.step_type == 'litho_patterning'
                          for ld in layer_names(s)}
                empty = sorted(ld for ld in layers if not layout.polygons(*ld))
                if empty:
                    print(f'WARNING: {tech_name}: mask layers {", ".join(f"{l}:{d}" for l, d in empty)} have no shapes '
                          f'in the clip window of {tech.layout.name} ({layout.format})', file=sys.stderr)

    def test_depo_materials_have_carbon_data(self):
        """Warns (does not fail) about deposited materials with no carbon data in config/materials.yaml."""
        mpw = Mpw(Config())
        unknown = defaultdict(int)
        for tech_name in TECHS:
            with self.subTest(tech=tech_name):
                for s in csvio.load_flow(Tech(tech_name).require().csv).steps:
                    for m in filter_materials(s.value('MaterialFilter')) if s.step_type.startswith('depo_') else []:
                        if not mpw.has_data(m):
                            unknown[m] += 1
        for m, n in sorted(unknown.items()):
            print(f'WARNING: {m} ({n} depo steps) has no carbon data in config/materials.yaml', file=sys.stderr)

    def test_flow_materials_are_spx_materials(self):
        """Every material a flow names is defined for spx (spx_colors in config/materials.yaml)."""
        spx = Config().spx_colors
        for tech_name in TECHS:
            with self.subTest(tech=tech_name):
                flow = csvio.load_flow(Tech(tech_name).require().csv)
                used = {m for s in flow.steps for col in s.params for m in step_materials(s.value(col))}
                self.assertFalse(sorted(used - set(spx)), f'{tech_name}: materials with no spx_colors entry')


    @unittest.skipUnless(shutil.which('zstd'), 'zstd not on PATH')
    def test_tdr_compressed_as_written(self):
        """A fake spx writes one tdr per step; each but the newest is compressed meanwhile, the
        rest on exit, and every .zst round-trips (so none was compressed half-written)."""
        with tempfile.TemporaryDirectory() as tmp:
            tdr_dir = Path(tmp)
            log = []
            progress = type('P', (), {'log': lambda self, m: log.append(m)})()
            data = {f'{i:04d}_step.tdr': (f'step {i} ' * 50000).encode() for i in range(1, 6)}

            def spx():
                for name, body in data.items():
                    with open(tdr_dir / name, 'wb') as f:  # written slowly, like spx
                        f.write(body[:len(body) // 2])
                        f.flush()
                        time.sleep(0.15)
                        f.write(body[len(body) // 2:])
                    time.sleep(0.15)

            with tdrzst.while_written(tdr_dir, progress, every=0.05):
                t = threading.Thread(target=spx)
                t.start()
                t.join()
                self.assertTrue(any(tdr_dir.glob('*.tdr.zst')), 'nothing compressed while spx ran')
            self.assertEqual(sorted(p.name for p in tdr_dir.iterdir()), sorted(n + '.zst' for n in data))
            for name, body in data.items():
                out = tdr_dir / ('out_' + name)
                tdrzst.decompress(tdr_dir / (name + '.zst'), out)
                self.assertEqual(out.read_bytes(), body)
                self.assertEqual(tdrzst.find(tdr_dir / name), tdr_dir / (name + '.zst'))
            self.assertIn('as written', log[-1])


if __name__ == '__main__':
    unittest.main()
