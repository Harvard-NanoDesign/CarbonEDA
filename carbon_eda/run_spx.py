# Author: David Kong
# License: MIT

"""Stage 2: run the generated route in spx batch mode.

Writes one tdr per step to outputs/<tech>/tdr/, compressed to <stem>.tdr.zst as soon as
spx moves on to the next step (see tdrzst), and the route TBSON to outputs/<tech>/tbson/.
Logs: outputs/<tech>/spx/run.out (spx console, one block per step as it runs),
outputs/<tech>/spx/run.log (spx's own log), and step counts in outputs/<tech>/progress.log.
"""

import json
import shlex
import subprocess
import sys

from . import tdrzst
from .paths import tool
from .progress import Progress


def command(tech):
    runner = tech.spx_dir / 'run.py'
    if not runner.exists():
        raise SystemExit(f'{runner} not found; run gen first')
    q = lambda p: shlex.quote(str(p))
    return (f'export PYTHONUNBUFFERED=1 && cd {q(tech.spx_dir)} && '
            + tool('spx', f'-nodisplay -exitOnError -log {q(tech.spx_dir / "run.log")} -runpy {q(runner)}'))


def run(tech, background=False):
    cmd = command(tech)
    out = tech.spx_dir / 'run.out'
    total = len(json.loads(tech.manifest.read_text())['steps'])
    progress = Progress(tech)
    progress.log(f'sim: spx started, {total} steps\n    console: {out}\n    spx log: {tech.spx_dir / "run.log"}'
                 f'\n    command: {cmd}')
    with open(out, 'w') as so:
        p = subprocess.Popen(['bash', '-lc', cmd], stdout=so, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=background)
        if background:
            progress.log(f'sim: spx running in background (pid {p.pid}); no step counts in this log, '
                         'tdr left uncompressed')
            return 0
        with progress.watch('sim', tech.tdr_dir, '*.tdr*', total), \
                tdrzst.while_written(tech.tdr_dir, progress):
            rc = p.wait()
    progress.log(f'sim: spx exited with {rc}')
    if rc:
        sys.stderr.write(''.join(open(out).readlines()[-20:]))
    return rc
