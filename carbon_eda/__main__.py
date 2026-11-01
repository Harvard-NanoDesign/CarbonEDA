# Author: David Kong
# License: MIT

"""carbon_eda: flow CSV + layout -> spx route, tdr per step, images, carbon, design-review PDF.

    python -m carbon_eda spx    TECH             gen + sim + vis (below)
    python -m carbon_eda report TECH             carbon from the CSV and config, then the PDF
                                                 (carbon CSVs only if spx has not run yet)
    python -m carbon_eda all    TECH             spx + report
  single stages:
    python -m carbon_eda gen    TECH             spx files + manifest + EPW/MPW/GPW
    python -m carbon_eda sim    TECH             spx batch run -> tdr per step
    python -m carbon_eda vis    TECH             svisual -> png per step
    python -m carbon_eda carbon TECH             EPW/MPW/GPW from the flow CSV only
    python -m carbon_eda fmt    TECH             rewrite the flow CSV in the standard column layout
                                                 (technologies/README.md); the flow is unchanged
    python -m carbon_eda spx-functions          list the installed spx's step functions in
                                                 config/spx_steps.yaml (after an spx upgrade)
"""

import argparse
import sys

from . import __version__
from .paths import Tech
from .progress import Progress


def _range(text):
    a, _, b = text.partition('-')
    return int(a), int(b or a)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='carbon_eda', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--version', action='version', version=f'CarbonEDA v{__version__}')
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('spx', 'report', 'all', 'gen', 'sim', 'vis', 'carbon'):
        p = sub.add_parser(name)
        p.add_argument('tech')
        if name in ('spx', 'report', 'all', 'gen', 'carbon'):
            p.add_argument('--limit', type=int, help='only the first N steps (quick checks)')
        if name == 'sim':
            p.add_argument('--background', action='store_true', help='detach instead of waiting for spx')
        if name in ('vis', 'report'):
            p.add_argument('--steps', type=_range, help='step index range, e.g. 1-20')
        if name in ('spx', 'all', 'gen'):
            p.add_argument('--zero-fill', action='store_true',
                           help='run steps with no spx parameters with 0 values without asking')
        if name in ('spx', 'all', 'vis'):
            p.add_argument('--jobs', type=int, default=1, help='svisual processes rendering at once (default 1)')
        if name == 'vis':
            p.add_argument('--missing', action='store_true', help='only steps that have no image yet')
    p = sub.add_parser('fmt', help='rewrite the flow CSV in the standard column layout')
    p.add_argument('tech')
    sub.add_parser('spx-functions', help="list the installed spx's step functions in config/spx_steps.yaml")
    a = ap.parse_args(argv)

    if a.cmd == 'spx-functions':
        from . import spxsteps
        spxsteps.dump_functions()
        return 0

    tech = Tech(a.tech).require()
    if a.cmd == 'fmt':
        from . import csvio
        cols = csvio.tidy(tech.csv)
        print(f'{tech.csv}: {len(cols)} columns')
        return 0
    progress = Progress(tech)
    progress.log(f'=== {a.cmd} {tech.name} (follow with: tail -f {progress.path}) ===')
    rc = 0
    stages = {'spx': ('gen', 'sim', 'vis'), 'all': ('gen', 'sim', 'vis', 'report')}.get(a.cmd, (a.cmd,))
    limit = getattr(a, 'limit', None)
    if 'gen' in stages:
        from . import carbon, generate
        flow = generate.run(tech, limit, a.zero_fill)
        _, tot = carbon.run(tech, flow)
        progress.log(f'gen: {len(flow.steps)} steps -> {tech.spx_dir}; carbon {tot["total_kgco2e"]:.2f} kgCO2e/wafer '
                     f'-> {tech.carbon_dir}')
    if 'sim' in stages:
        from . import run_spx
        rc = run_spx.run(tech, getattr(a, 'background', False))
        if rc:
            progress.log(f'=== {a.cmd} {tech.name} stopped: spx failed (exit {rc}), see {tech.spx_dir / "run.out"} ===')
            return rc
    if 'vis' in stages:
        from . import render
        rc = render.run(tech, getattr(a, 'steps', None), a.jobs, getattr(a, 'missing', False))
    # carbon is recomputed from the current CSV and config/ (seconds) before the report
    if 'carbon' in stages or 'report' in stages:
        from . import carbon, csvio
        flow = csvio.load_flow(tech.csv)
        if limit:
            flow.truncate(limit)
        _, tot = carbon.run(tech, flow)
        progress.log(f'carbon: {len(flow.steps)} steps; {tot["total_kgco2e"]:.2f} kgCO2e/wafer -> {tech.carbon_dir}')
    if 'report' in stages:
        if tech.manifest.is_file():
            from .report import run
            progress.log('report: started')
            out = run(tech, getattr(a, 'steps', None))
            progress.log(f'report: {out}')
        else:
            progress.log(f'report: no spx run yet ({tech.manifest} missing), so carbon CSVs only; '
                         f'make spx TECH={tech.name} for the PDF')
    progress.log(f'=== {a.cmd} {tech.name} finished (exit {rc}) ===')
    return rc


if __name__ == '__main__':
    sys.exit(main())
