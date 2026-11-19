# Author: David Kong
# License: MIT

"""Stage 3: svisual renders every step's tdr to png (svisual_render.tcl).

View settings come from the render: section of tech.yaml. Output:
outputs/<tech>/img/<stem>.3d.png, <stem>.fit.png (the same view zoomed to fit the step's
structure, for the report's inset) and <stem>.cut<i>-<axis>.png per cut plane.

Every step is drawn at the same scale: render.zoom_box in tech.yaml, else a box
fitted to the final structure (x and y from its tdr bounding box, z from its solid
layers). Its z window starts at the wafer bottom of the first step, or, from a
flip_structure step on, sits where the flipped final structure is; a step taller
than the final structure (e.g. an overfill before CMP) is cut off at the top, and
the .fit.png inset shows it whole. The pre-flip box is written to
outputs/<tech>/img/zoom_box.txt. Without render.cuts, each step also gets an x and a
y cut through the middle of its box, each zoomed to fit that step's cross section.

A compressed tdr (.tdr.zst) is decompressed to outputs/<tech>/img/tdr_tmp/ for its step
and removed after it.

Logs: outputs/<tech>/img/render.log (svisual console, one line per step) and
step counts in outputs/<tech>/progress.log. With --jobs N the steps are split into N
ranges, each rendered by its own svisual on its own display at the same time
(render_job<k>.tcl, render<k>.log, tdr_tmp<k>/).

svisual needs an X display to export images. It uses $DISPLAY when set (as
`svisual -tcl`), else a private headless Xvnc display, else `svisual -batchx`
(which needs Xvfb on PATH); see display: in config/tools.yaml.
"""

import contextlib
import json
import tempfile
import threading
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import tdrzst
from .carbon import Config
from .paths import TCL_DIR, tool, tools_config
from .progress import Progress


def tcl_str(text):
    return '"' + re.sub(r'([\\"$\[\]])', r'\\\1', str(text).replace('\n', ' ')) + '"'


FIT_RESOLUTION = '640x480'
# svisual grows by ~0.15 GB per step rendered, so each worker restarts it after this many steps
BATCH_STEPS = 50
# a step takes ~10-20 s; this long with no console output means svisual hung (tools.yaml svisual.step_timeout)
STEP_TIMEOUT = 300
CUT_HEIGHT_PX = 768


def structure_box(tdrs):
    """[x0, x1, y0, y1, z0, z1] (um) holding the solid structure of every tdr given, or None.

    Reads spx's column geometry: bbox_first/bbox_second give x and y, and the
    k-coordinates (z levels of the layers below the gas) give z.
    """
    try:
        import h5py
    except ImportError:
        return None
    box = None
    with tempfile.TemporaryDirectory() as tmp:
        for tdr in tdrs:
            path = tdr
            if tdr.suffix == tdrzst.ZST:
                path = Path(tmp) / tdr.stem
                tdrzst.decompress(tdr, path)
            try:
                with h5py.File(path, 'r') as f:
                    g = f['collection/gc_geometry_0']
                    lo, hi, k = g['bbox_first'][()], g['bbox_second'][()], g['k-coordinates'][()]
            except (OSError, KeyError):
                continue
            finally:
                if path != tdr:
                    path.unlink(missing_ok=True)
            b = [float(lo[0]), float(hi[0]), float(lo[1]), float(hi[1]), float(min(k)), float(max(k))]
            box = b if box is None else [min(box[i], b[i]) if i % 2 == 0 else max(box[i], b[i]) for i in range(6)]
    if box:
        pad = 0.05 * (box[5] - box[4])
        box[4] -= pad
        box[5] += pad
    return box


def default_cuts(box):
    """An x and a y cut through the middle of the zoom box; no range, so svisual fits each to the step."""
    x0, x1, y0, y1, z0, z1 = box
    return [{'axis': 'x', 'at': (x0 + x1) / 2}, {'axis': 'y', 'at': (y0 + y1) / 2}]


def cut_specs(tech):
    """The cut planes vis draws for a technology (tech.yaml render.cuts, else the default x and y cuts)."""
    cfg = tech.cfg.get('render') or {}
    if cfg.get('cuts'):
        return cfg['cuts']
    zoom = cfg.get('zoom_box')
    saved = tech.img_dir / 'zoom_box.txt'
    if not zoom and saved.exists():
        zoom = [float(v) for v in saved.read_text().split()]
    return default_cuts(zoom) if zoom else []


def _cut_resolution(c):
    """Export size matching the cut's ranges, so svisual's fixed aspect ratio shows all of them."""
    r = c.get('range') or {}
    if len(r) != 2:
        return ''
    (a0, a1), (b0, b1) = list(r.values())
    ratio = abs(a1 - a0) / abs(b1 - b0) if b1 != b0 else 1.0
    return f'{max(420, min(2048, round(CUT_HEIGHT_PX * ratio)))}x{CUT_HEIGHT_PX}'


def _tcl_cuts(cuts):
    out = []
    for c in cuts:
        ranges = ' '.join(f'{{{ax} {lo} {hi}}}' for ax, (lo, hi) in (c.get('range') or {}).items())
        out.append(f'{{{c["axis"]} {float(c["at"])} {{{ranges}}} {{{_cut_resolution(c)}}}}}')
    return ' '.join(out)


def _tcl_box(box):
    return '{' + ' '.join(str(float(z)) for z in box or []) + '}'


def material_colors():
    """The spx colors (config/materials.yaml spx_colors) as tcl dict items, so svisual draws each
    material as spx does: svisual's datexcodes.txt knows only standard materials and draws the others
    (SiCOH, SiGeB, Si:P, Oxide2, ...) all in one grey, and some (Tungsten, TiN) in other colors.
    Gas keeps svisual's own."""
    return ' '.join(f'{tcl_str(m)} {tcl_str(c)}' for m, c in Config().spx_colors.items() if c and m != 'Gas')


def job_tcl(jobs, img_dir, render_cfg, zoom=None, cuts_cfg=None, tmp_dir=None):
    """jobs: [(tdr_path, title, stem)] or [(tdr_path, title, stem, zoom box, cuts)] to give a step its
    own box and cuts; tdr_path may be a .tdr.zst"""
    cuts = _tcl_cuts(cuts_cfg if cuts_cfg is not None else render_cfg.get('cuts') or [])
    zoom = zoom or render_cfg.get('zoom_box') or []
    lines = ['# Generated by carbon_eda; run with: svisual -tcl <this file> (or -batchx)',
             f'set img_dir {tcl_str(img_dir)}',
             f'set tmp_dir {tcl_str(tmp_dir or img_dir / "tdr_tmp")}',
             f'set zstd {tcl_str(shutil.which("zstd") or "zstd")}',
             f'set resolution {tcl_str(render_cfg.get("resolution", "1024x768"))}',
             f'set fit_resolution {tcl_str(render_cfg.get("fit_resolution", FIT_RESOLUTION))}',
             f'set zoom_box {_tcl_box(zoom)}',
             f'set cuts [list {cuts}]',
             f'set material_colors [dict create {material_colors()}]',
             'set job_steps [list]']
    for t, title, stem, *own in jobs:
        box, step_cuts = own or (None, None)
        lines.append(f'lappend job_steps [list {tcl_str(t)} {tcl_str(title)} {tcl_str(stem)} {_tcl_box(box)} '
                     f'[list {_tcl_cuts(step_cuts or [])}]]')
    lines.append(f'source {tcl_str(TCL_DIR / "svisual_render.tcl")}')
    return '\n'.join(lines) + '\n'


def title(step):
    return f'{step["index"]}. {step["module"]}: {step["step"]}'


@contextlib.contextmanager
def display():
    """Yields (DISPLAY value or None, svisual mode flag)."""
    if os.environ.get('DISPLAY'):
        yield os.environ['DISPLAY'], '-tcl'
        return
    cfg = tools_config().get('display', {}) or {}
    xvnc = cfg.get('xvnc') or ''
    if not xvnc or not Path(xvnc).exists():
        yield None, '-batchx'
        return
    lo, hi = cfg.get('numbers', [400, 999])
    n = next(n for n in range(lo, hi + 1)
             if not Path(f'/tmp/.X11-unix/X{n}').exists() and not Path(f'/tmp/.X{n}-lock').exists())
    # No VNC port and no TCP listener: just a local framebuffer for this run.
    proc = subprocess.Popen([xvnc, f':{n}', '-geometry', '2560x1440', '-depth', '24', '-rfbport', '-1',
                             '-SecurityTypes', 'None', '-nolisten', 'tcp'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if Path(f'/tmp/.X11-unix/X{n}').exists():
                break
            time.sleep(0.1)
        yield f':{n}', '-tcl'
    finally:
        proc.terminate()
        proc.wait()


def _svisual(cmd, f, log, timeout):
    """Run one svisual (cmd), its console appended to log through f. svisual sometimes hangs after
    a crash instead of exiting, so once it has started a step, `timeout` s with no console output
    kills it. Returns (exit code, whether it was killed, its console text)."""
    f.flush()
    start = log.stat().st_size
    p = subprocess.Popen(['bash', '-lc', cmd], stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)

    def console():
        with open(log) as r:
            r.seek(start)
            return r.read()

    size, last, killed = start, time.monotonic(), False
    while True:
        try:
            p.wait(5)
            break
        except subprocess.TimeoutExpired:
            pass
        now = log.stat().st_size
        if now != size:
            size, last = now, time.monotonic()
        elif timeout and time.monotonic() - last > timeout and 'rendering ' in console():
            killed = True
            # the whole process group: bash, the svisual launcher and svisual_exec
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, sig)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.wait(10)
            p.wait()
            break
    f.flush()
    return p.returncode, killed, console()


def render(jobs, img_dir, render_cfg, progress=None, zoom=None, cuts=None, parallel=1, batch=BATCH_STEPS):
    """Render jobs with `parallel` workers at once, each on its own contiguous range of steps and display,
    running svisual on `batch` steps at a time (svisual's memory grows with every step it renders).
    An svisual that crashes or hangs on a step is restarted from that step; a step it fails on twice
    is skipped (vis --missing renders it later)."""
    img_dir.mkdir(parents=True, exist_ok=True)
    n = max(1, min(parallel, len(jobs)))
    size = -(-len(jobs) // n)
    chunks = [jobs[i:i + size] for i in range(0, len(jobs), size)]
    suffix = (lambda k: '') if len(chunks) == 1 else str
    say = progress.log if progress else print
    sv = (tools_config().get('tools') or {}).get('svisual') or {}
    timeout = sv.get('step_timeout', STEP_TIMEOUT)
    results = {}  # worker -> (rendered, worst exit code)

    def work(k, chunk, disp, mode):
        tmp, job, log = (img_dir / f'{name}{suffix(k)}{ext}' for name, ext in
                         (('tdr_tmp', ''), ('render_job', '.tcl'), ('render', '.log')))
        cmd = f'cd {shlex.quote(str(img_dir))} && '
        if disp:
            cmd += f'export DISPLAY={shlex.quote(disp)} && '
        cmd += tool('svisual', f'{mode} {shlex.quote(str(job))}')
        rendered, worst, pending, retried = 0, 0, list(chunk), set()
        with open(log, 'w') as f:
            while pending:
                part = pending[:batch]
                job.write_text(job_tcl(part, img_dir, render_cfg, zoom, cuts, tmp))
                rc, killed, out = _svisual(cmd, f, log, timeout)
                done = re.findall(r'rendered (\d+) of \d+ steps', out)
                if done:
                    rendered += int(done[-1])
                    worst = max(worst, rc)
                    pending = pending[len(part):]
                    continue
                # it stopped on the last step it started; the ones before that are done
                started = [int(i) for i in re.findall(r'rendering (\d+)/\d+:', out)]
                if not started:
                    worst = max(worst, rc, 1)
                    say(f'vis: svisual exited with {rc} before rendering a step; skipped steps '
                        f'{part[0][2][:4]}-{part[-1][2][:4]} (render them with vis --missing)')
                    pending = pending[len(part):]
                    continue
                at = started[-1]
                rendered += len(started) - 1
                stem = part[at - 1][2]
                how = f'hung (no output for {timeout} s), killed' if killed else f'exited with {rc}'
                if stem in retried:
                    worst = max(worst, rc, 1)
                    say(f'vis: svisual {how} on {stem} again; skipped (render it with vis --missing)')
                    pending = pending[at:]
                else:
                    say(f'vis: svisual {how} on {stem}; restarting from it')
                    retried.add(stem)
                    pending = pending[at - 1:]
        shutil.rmtree(tmp, ignore_errors=True)
        results[k] = (rendered, worst)

    with contextlib.ExitStack() as stack:
        workers = []
        for k, chunk in enumerate(chunks):
            disp, mode = stack.enter_context(display())
            if mode == '-batchx' and not shutil.which('Xvfb'):
                print('warning: no $DISPLAY, no Xvnc (config/tools.yaml) and no Xvfb; svisual -batchx will likely fail')
            say(f'vis: svisual {mode} started (display {disp or "virtual"}), {len(chunk)} steps'
                + (f' ({chunk[0][2][:4]}-{chunk[-1][2][:4]})' if len(chunks) > 1 else '')
                + f', {batch} per svisual run\n    console: {img_dir / f"render{suffix(k)}.log"}')
            workers.append(threading.Thread(target=work, args=(k, chunk, disp, mode)))
            workers[-1].start()
        watch = progress.watch('vis', img_dir, '*.3d.png', len(jobs)) if progress else contextlib.nullcontext()
        with watch:
            for w in workers:
                w.join()
    rendered = sum(r for r, _ in results.values())
    rc = max((c for _, c in results.values()), default=1)
    say(f'vis: svisual exited with {rc}; rendered {rendered} of {len(jobs)} steps')
    return rc


def run(tech, steps=None, parallel=1, missing=False):
    manifest = json.loads(tech.manifest.read_text())
    selected = [s for s in manifest['steps'] if not steps or steps[0] <= s['index'] <= steps[1]]
    if missing:
        selected = [s for s in selected if not (tech.img_dir / f'{s["stem"]}.3d.png').exists()]
        if not selected:
            print(f'{tech.img_dir}: every step has its images')
            return 0
    # plain or compressed tdr; a step with neither is skipped by svisual_render.tcl
    jobs = [(tdrzst.find(tech.outputs / s['tdr']) or tech.outputs / s['tdr'], title(s), s['stem'])
            for s in selected]
    missing = sum(not j[0].exists() for j in jobs)
    if missing == len(jobs):
        raise SystemExit(f'no tdr files in {tech.tdr_dir}; run sim first')
    if missing:
        print(f'{missing} of {len(jobs)} steps have no tdr yet; they are skipped')
    render_cfg = tech.cfg.get('render') or {}
    progress = Progress(tech)
    zoom = render_cfg.get('zoom_box')
    if not zoom:
        # from the whole flow's first and last tdr, so a --steps range gets the same zoom as the full render
        every = [tdrzst.find(tech.outputs / s['tdr']) for s in manifest['steps']]
        every = [t for t in every if t]
        first, final = (structure_box(every[:1]), structure_box(every[-1:])) if every else (None, None)
        if first and final:
            height = final[5] - final[4]
            zoom = final[:4] + [first[4], first[4] + height]
            flips = [s['index'] for s in manifest['steps'] if s['step_type'] == 'flip_structure']
            boxes = {s['index']: final if flips and s['index'] >= flips[-1] else zoom for s in manifest['steps']}
            tech.img_dir.mkdir(parents=True, exist_ok=True)
            (tech.img_dir / 'zoom_box.txt').write_text(' '.join(f'{v:.4f}' for v in zoom) + '\n')
            progress.log(f'vis: zoom box {[round(v, 4) for v in zoom]} um (x0 x1 y0 y1 z0 z1), fitted to the '
                         'final structure' + (f'; from step {flips[-1]} (flip) {[round(v, 4) for v in final]}'
                                              if flips else ''))
            if not render_cfg.get('cuts'):
                jobs = [j + (boxes[s['index']], default_cuts(boxes[s['index']])) for j, s in zip(jobs, selected)]
            else:
                jobs = [j + (boxes[s['index']], None) for j, s in zip(jobs, selected)]
    cuts = render_cfg.get('cuts') or (default_cuts(zoom) if zoom else [])
    return render(jobs, tech.img_dir, render_cfg, progress, zoom, cuts, parallel)
