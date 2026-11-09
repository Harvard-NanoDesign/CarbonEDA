# Author: David Kong
# License: MIT

"""Stage 4: the design-review PDF, one 13.33 x 7.5 in (16:9 slide) page per process step.

Pages: cover, CarbonEDA parameters (energy, gases, materials), one page per
step, then summaries. A step page shows the structure after the step (3D, x cut
and y cut at the same zoom on every page), the mask a litho step exposes, the
step's carbon, and the flow's EPW/MPW/GPW, process steps, materials, gases and
embodied carbon up to it.
Output: outputs/<tech>/report/<tech>_design_review.pdf
"""

import csv
import datetime
import json
import math
import re
import subprocess
from collections import defaultdict

from . import __version__, csvio, spxsteps
from .carbon import Config, step_count, uncounted
from .carbon.epw import overhead_fraction
from .carbon.mpw import Mpw
from .layout import Layout, layer_names
from .paths import rel, tools_config

PAGE = (13.333, 7.5)
# Chart chrome and ink.
SURFACE, INK, INK2, MUTED = '#fcfcfb', '#0b0b0b', '#52514e', '#898781'
GRID, AXIS = '#e1e0d9', '#c3c2b7'
# EPW, MPW and GPW colors, and a light track of each.
COLORS = {'epw': '#2a78d6', 'mpw': '#eb6834', 'gpw': '#1baf7a'}
TRACKS = {'epw': '#dbe8f8', 'mpw': '#fbe1d5', 'gpw': '#d3f0e4'}
LABELS = {'epw': 'EPW  energy', 'mpw': 'MPW  materials', 'gpw': 'GPW  gases'}
ACCENT = '#6250d6'      # "this step" on every chart
REST = '#d6d5ce'        # everything that isn't this step
OVERHEAD_HATCH = '////'  # EPW facility overhead: the EPW color shaded with surface-colored hatching
FONT = 'DejaVu Sans'


def _read(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _fmt(x, unit=''):
    """Compact number: 0, 0.00123, 0.201, 3.98, 194.5, 12.3K."""
    x = _f(x)
    if x == 0:
        s = '0'
    elif abs(x) >= 10000:
        s = f'{x / 1000:,.1f}K'
    elif abs(x) >= 100:
        s = f'{x:,.1f}'
    elif abs(x) >= 1e-4:
        s = f'{x:.3g}'
    else:
        s = f'{x:.2e}'
    return f'{s} {unit}'.strip()


def _clip(text, n):
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + '…'


def _wrap(text, width, lines=2):
    words, out, line = str(text).split(), [], ''
    for w in words:
        if line and len(line) + 1 + len(w) > width:
            out.append(line)
            line = w
        else:
            line = f'{line} {w}'.strip()
    out.append(line)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = _clip(out[-1], width - 1) + '…'
    return '\n'.join(out)


def _ranges(nums):
    """[3, 4, 5, 9] -> '3-5, 9'"""
    out, start = [], None
    for k, n in enumerate(nums):
        if start is None:
            start = n
        if k + 1 == len(nums) or nums[k + 1] != n + 1:
            out.append(f'{start}-{n}' if n != start else str(n))
            start = None
    return ', '.join(out)


def _accumulate(seq, as_pairs):
    acc = defaultdict(float)
    for d in seq:
        for k, v in (d if as_pairs else d.items()):
            acc[k] += v
    return acc


def file_version(path):
    """'<git hash> <date>' of the last commit touching path (+ ', modified'), else its mtime date."""
    try:
        log = subprocess.run(['git', 'log', '-1', '--format=%h %cs', '--', path.name], cwd=path.parent,
                             capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(['git', 'status', '--porcelain', '--', path.name], cwd=path.parent,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        if log:
            return log + (', modified' if dirty else '')
    except (OSError, subprocess.SubprocessError):
        pass
    return datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()


def tool_version():
    t = (tools_config().get('tools') or {}).get('spx') or {}
    m = re.search(r'spx/(\S+)', t.get('setup') or '')
    return f'spx {m.group(1)}' if m else (t.get('command') or 'spx')


# ---------------------------------------------------------------- drawing helpers

def _new_page(plt):
    return plt.figure(figsize=PAGE, facecolor=SURFACE)


def _hline(fig, x0, x1, y, color=GRID, lw=0.6):
    from matplotlib.lines import Line2D
    fig.add_artist(Line2D([x0, x1], [y, y], color=color, lw=lw, transform=fig.transFigure))


def _label(fig, x, y, text, size=8.5):
    """Section label: small, secondary ink, sentence case."""
    return fig.text(x, y, text, fontsize=size, color=INK2, va='bottom', fontweight='bold')


def _quiet(ax, keep=('bottom',)):
    """Recessive axes: hairline baseline only, muted ticks, no box."""
    ax.set_facecolor(SURFACE)
    for side, spine in ax.spines.items():
        spine.set_visible(side in keep)
        spine.set_color(AXIS)
        spine.set_linewidth(0.8)
    ax.tick_params(labelsize=6.5, colors=MUTED, length=0, pad=2)


def _table(fig, x, y, widths, head, body, size=8, row=0.032, bold_first=False):
    """Hairline table drawn as text: header in secondary ink, a hairline under it and between rows.
    widths are column widths in figure fraction; returns the y below the last row."""
    xs = [x]
    for w in widths[:-1]:
        xs.append(xs[-1] + w)
    right = x + sum(widths)
    if head:
        for cx, h in zip(xs, head):
            fig.text(cx, y - row * 0.6, h, fontsize=size - 0.5, color=INK2, fontweight='bold', va='center')
        y -= row
        _hline(fig, x, right, y, color=AXIS, lw=0.8)
    for r in body:
        for k, (cx, v) in enumerate(zip(xs, r)):
            fig.text(cx, y - row * 0.5, v, fontsize=size, color=INK, va='center',
                     fontweight='bold' if bold_first and k == 0 else 'normal')
        y -= row
        _hline(fig, x, right, y)
    return y


# ---------------------------------------------------------------- data

class Flow:
    """Everything the pages need, per step and cumulative."""

    def __init__(self, tech, flow, rows, gases, totals, cfg, carriers=(), spx=None):
        """rows: the flow steps' carbon rows; carriers: the carrier wafers' (made first, so counted from step 1);
        spx: the manifest's steps by index (how spx ran each one)."""
        self.tech, self.flow, self.rows, self.totals, self.cfg = tech, flow, rows, totals, cfg
        self.spx = spx or {}
        self.n = len(rows)
        self.skip = uncounted(cfg)
        self.count = step_count(cfg)
        self.by_index = {s.index: s for s in flow.steps}
        self.mpw = mpw = Mpw(cfg)
        # per step: deposited materials [(canonical, mass_g)] (gwp_factor 0 ones left out), gases {name: g} supplied / emitted
        self.mats, self.supplied, self.emitted = [], [], []
        for r in rows:
            s = self.by_index[int(r['index'])]
            self.mats.append([(m, g) for m, g, c in mpw.step(s)['materials'] if g > 0 and c > 0])
            sup, emi = defaultdict(float), defaultdict(float)
            for g in gases.get(int(r['index']), []):
                sup[g['gas']] += _f(g['supplied_g'])
                if g['role'] != 'upstream':
                    emi[g['gas']] += _f(g.get('emitted_g'))
            self.supplied.append({k: v for k, v in sup.items() if v})
            self.emitted.append({k: v for k, v in emi.items() if v})
        final_mats = _accumulate(self.mats, True)
        self.material_names = list(dict.fromkeys(m for d in self.mats for m, _ in d))
        self.top_materials = sorted(final_mats, key=lambda m: -final_mats[m])   # every material, heaviest first
        final_gas = _accumulate(self.emitted, False)
        self.emitted_gases = sorted(final_gas, key=lambda g: -final_gas[g])
        areas = [r['process_area'] for r in rows]
        known = list(cfg.carbon['energy_per_step_kwh']) + ['Wafer']
        self.areas = [a for a in known if a in areas and a not in self.skip] + \
            [a for a in dict.fromkeys(areas) if a not in known and a not in self.skip]
        self.cum = {k: [] for k in ('epw', 'mpw', 'gpw', 'total')}
        acc = defaultdict(float, {k: sum(_f(r[f'{k}_kgco2e']) for r in carriers) for k in self.cum})
        for r in rows:
            for k in ('epw', 'mpw', 'gpw', 'total'):
                acc[k] += _f(r[f'{k}_kgco2e'])
                self.cum[k].append(acc[k])

    def material_key(self, m):
        return m


# ---------------------------------------------------------------- step page

class StepPage:
    """One figure reused for every step page: built once, then updated per step."""

    L0, L1 = 0.03, 0.555          # left column: structure images
    R0, R1 = 0.59, 0.972          # right column: numbers and charts

    def __init__(self, plt, fl, cuts, layout, versions, layout_error=''):
        import numpy as np
        self.np, self.plt, self.fl = np, plt, fl
        self.layout, self.layout_error = layout, layout_error
        self.fig = fig = _new_page(plt)
        L0, L1, R0, R1 = self.L0, self.L1, self.R0, self.R1

        # header: step name on the left, its carbon as stat tiles on the right
        self.kicker = fig.text(L0, 0.945, '', fontsize=8.5, color=MUTED, va='bottom')
        self.title = fig.text(L0, 0.895, '', fontsize=19, fontweight='bold', color=INK, va='bottom')
        self.subtitle = fig.text(L0, 0.868, '', fontsize=9, color=INK2, va='bottom')
        self.tiles = []
        for k, (x, label) in enumerate(((R0, 'this step (kgCO2e)'), (R0 + 0.13, 'flow so far (kgCO2e)'),
                                        (R0 + 0.26, 'share of the flow'))):
            self.tiles.append(fig.text(x, 0.895, '', fontsize=19, color=INK, va='bottom'))
            fig.text(x, 0.873, label, fontsize=8, color=INK2, va='bottom')
        _hline(fig, L0, R1, 0.855, color=AXIS, lw=0.8)

        # left: 3D view, then x cut, y cut and mask
        _label(fig, L0, 0.825, '3D structure after this step')
        self.ax3d = self._image_ax([L0, 0.37, L1 - L0, 0.45])
        # inset: the same view zoomed to fit this step's structure (top left, clear of the axis triad)
        self.fit_label = _label(fig, L0 + 0.004, 0.795, 'zoomed to fit', size=7)
        self.axfit = self._image_ax([L0 + 0.004, 0.6, 0.16, 0.19], frame=True)
        tw = (L1 - L0 - 0.03) / 3
        cut_titles = [f'{c["axis"]} cut at {float(c["at"]):.3f} µm' for c in cuts[:2]]
        cut_titles += ['x cut', 'y cut'][len(cut_titles):]
        self.axcut = []
        for k, t in enumerate(cut_titles):
            x = L0 + k * (tw + 0.015)
            _label(fig, x, 0.325, t)
            self.axcut.append(self._image_ax([x, 0.06, tw, 0.26], frame=True))
        x = L0 + 2 * (tw + 0.015)
        self.mask_title = _label(fig, x, 0.325, 'mask for this step')
        self.axgds = fig.add_axes([x, 0.06, tw, 0.26])

        # right: this step's numbers, two key/value columns with hairlines
        _label(fig, R0, 0.825, 'this step')
        keys = [('process area', 'material', 'thickness', 'material GWP', 'input gases', 'energy'),
                ('EPW', 'EPW tool', 'EPW overhead', 'MPW', 'GPW', 'embodied carbon')]
        self.info = {}
        half = (R1 - R0) / 2
        for c, col in enumerate(keys):
            x = R0 + c * (half + 0.01)
            for r, key in enumerate(col):
                y = 0.8 - r * 0.023
                fig.text(x, y, key, fontsize=7.5, color=INK2, va='center')
                self.info[key] = fig.text(x + 0.075, y, '', fontsize=7.5, color=INK, va='center',
                                          fontweight='bold' if key == 'embodied carbon' else 'normal')
                _hline(fig, x, x + half - 0.01, y - 0.0115)
        self.notes = fig.text(R0, 0.655, '', fontsize=7, color=INK2, va='top', linespacing=1.45)

        # carbon so far: one meter per EPW / MPW / GPW, track = the whole flow
        _label(fig, R0, 0.585, 'carbon so far, of the whole flow (kgCO2e)')
        self.meters = {}
        for k, key in enumerate(('epw', 'mpw', 'gpw')):
            y = 0.56 - k * 0.036
            fig.text(R0, y + 0.012, LABELS[key], fontsize=7.5, color=INK, va='bottom')
            ax = fig.add_axes([R0, y - 0.004, R1 - R0, 0.013])
            ax.set_axis_off()
            total = fl.cum[key][-1] or 1.0
            ax.set_xlim(0, total)
            ax.set_ylim(0, 1)
            ax.barh([0.5], [total], height=1, color=TRACKS[key])
            past = ax.barh([0.5], [0], height=1, color=COLORS[key])[0]
            now = ax.barh([0.5], [0], height=1, color=ACCENT)[0]
            caret, = ax.plot([], [], marker='v', ms=5.5, color=ACCENT, clip_on=False, ls='none')
            value = fig.text(R1, y + 0.012, '', fontsize=7.5, color=INK2, va='bottom', ha='right')
            self.meters[key] = (past, now, caret, value, total)

        # cumulative embodied carbon
        _label(fig, R0, 0.445, 'embodied carbon up to each step (kgCO2e per wafer)')
        self.axline = ax = fig.add_axes([R0 + 0.03, 0.355, R1 - R0 - 0.03, 0.08])
        _quiet(ax, keep=('bottom',))
        xs = np.arange(1, fl.n + 1)
        ax.plot(xs, fl.cum['total'], color=GRID, lw=1.5, solid_capstyle='round')
        self.line_past, = ax.plot([], [], color=COLORS['epw'], lw=2, solid_capstyle='round',
                                  solid_joinstyle='round')
        self.dot, = ax.plot([], [], 'o', ms=8, color=ACCENT, markeredgecolor=SURFACE, markeredgewidth=2, zorder=5)
        ax.set_xlim(0, max(fl.n, 2) * 1.01)
        top = fl.cum['total'][-1] or 1.0
        ax.set_ylim(0, top * 1.12)
        ax.set_yticks([0, top / 2, top])
        ax.set_yticklabels(['0', _fmt(top / 2), _fmt(top)])
        ax.grid(axis='y', color=GRID, lw=0.6)
        ax.set_axisbelow(True)

        # breakdowns up to this step (this step's share in the accent, the rest gray): every process area,
        # material and gas of the flow, so the charts take the rest of the column
        w = (R1 - R0 - 0.04) / 3
        specs = [('steps by process area', fl.areas, 'count'),
                 ('materials deposited (g)', fl.top_materials, 'mass'),
                 ('gases emitted (g)', fl.emitted_gases, 'mass')]
        self.breakdowns = []
        for k, (title, names, kind) in enumerate(specs):
            x = R0 + k * (w + 0.02)
            _label(fig, x, 0.315, title, size=7.5)
            ax = fig.add_axes([x + 0.047, 0.035, w - 0.047, 0.27])
            self.breakdowns.append(self._hbars(ax, names, kind))

        fig.text(L0, 0.012, f'flow {fl.tech.csv.name} ({versions["csv"]})   ·   layout {fl.tech.layout.name} '
                 f'({versions["layout"]})   ·   {versions["tool"]}   ·   carbon intensity '
                 f'{_f(fl.totals["ci_fab_kgco2e_per_kwh"]):g} kgCO2e/kWh   ·   gases after abatement',
                 fontsize=6.5, color=MUTED)
        fig.text(R1, 0.012, f'CarbonEDA v{__version__}  ·  {fl.tech.name}  ·  generated {versions["generated"]}', fontsize=6.5,
                 color=MUTED, ha='right')

    def _image_ax(self, rect, frame=False):
        ax = self.fig.add_axes(rect)
        ax.set_xticks([])
        ax.set_yticks([])
        for s in ax.spines.values():
            s.set_visible(frame)
            s.set_color(GRID)
        ax.set_facecolor(SURFACE)
        img = ax.imshow(self.np.ones((2, 2, 3)), interpolation='antialiased')
        msg = ax.text(0.5, 0.5, '', ha='center', va='center', fontsize=8.5, color=MUTED, transform=ax.transAxes)
        return ax, img, msg

    def _hbars(self, ax, names, kind):
        _quiet(ax, keep=())
        ys = list(range(len(names)))[::-1]
        bars = ax.barh(ys, [0] * len(names), height=0.62, color=REST)
        # labels sized to the row pitch (points per row of the axes), 4.5-6.5 pt
        pitch = ax.get_position().height * PAGE[1] * 72 / max(len(names), 1)
        size = min(6.5, max(4.5, 0.75 * pitch))
        ax.set_yticks(ys)
        ax.set_yticklabels([_clip(n.replace('_', ' '), 14) for n in names], fontsize=size, color=INK2)
        ax.set_xticks([])
        ax.set_ylim(-0.6, max(len(names), 1) - 0.4)
        vals = [ax.text(0, y, '', fontsize=size - 0.5, color=INK2, va='center') for y in ys]
        return {'ax': ax, 'names': names, 'bars': bars, 'vals': vals, 'kind': kind}

    def _set_hbars(self, b, totals, current):
        top = max([totals.get(n, 0.0) for n in b['names']] + [1e-12])
        b['ax'].set_xlim(0, top * 1.45)
        for name, bar, val in zip(b['names'], b['bars'], b['vals']):
            v = totals.get(name, 0.0)
            bar.set_width(v)
            bar.set_color(ACCENT if name in current else REST)
            val.set_x(v + top * 0.04)
            val.set_text('' if not v else str(math.ceil(v)) if b['kind'] == 'count' else _fmt(v))
            val.set_color(INK if name in current else INK2)
            val.set_fontweight('bold' if name in current else 'normal')

    def _show(self, slot, path, missing):
        ax, img, msg = slot
        if path and path.exists():
            import matplotlib.image as mpimg
            data = mpimg.imread(path)
            h, w = data.shape[:2]
            img.set_data(data)
            img.set_extent((0, w, h, 0))
            ax.set_xlim(0, w)
            ax.set_ylim(h, 0)
            img.set_visible(True)
            msg.set_text('')
        else:
            img.set_visible(False)
            msg.set_text(missing)

    def _mask(self, step):
        ax = self.axgds
        ax.clear()
        ax.set_xticks([])
        ax.set_yticks([])
        for s in ax.spines.values():
            s.set_color(GRID)
        layers = layer_names(step) if step.step_type == 'litho_patterning' else []
        if not layers or not self.layout:
            ax.set_facecolor(SURFACE)
            why = 'no mask for this step' if not layers else _wrap(f'layout not readable: {self.layout_error}', 40, 4)
            ax.text(0.5, 0.5, why, ha='center', va='center', color=MUTED, fontsize=8, transform=ax.transAxes)
            self.mask_title.set_text('mask for this step')
            return
        from matplotlib.collections import PolyCollection
        ax.set_facecolor('#1b1b1a')
        x0, y0, x1, y1 = self.layout.clip
        n = 0
        for i, (l, d) in enumerate(layers):
            polys = self.layout.polygons(l, d)
            n += len(polys)
            if polys:
                ax.add_collection(PolyCollection(polys, facecolor=['#ff6b5e', '#8fd16f', '#6fb7ff', '#ffd166'][i % 4],
                                                 edgecolor='none'))
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        ax.set_aspect('equal', adjustable='box', anchor='N')
        pol = step.cells.get('Polarity')
        label = ', '.join(f'{l}:{d}' for l, d in layers)
        self.mask_title.set_text(f'mask: layer {label}' + (f', {pol.lower()}' if pol else '')
                                 + f'  ({self.layout.format})')
        if not n:
            ax.text(0.5, 0.5, 'nothing in the clip window', ha='center', va='center', color='white', fontsize=7.5,
                    transform=ax.transAxes)

    def update(self, i, images):
        """Draw step row i (0-based) of the flow; images: (3d png, [cut pngs])."""
        fl = self.fl
        r = fl.rows[i]
        idx = int(r['index'])
        s = fl.by_index[idx]
        self.kicker.set_text(f'STEP {idx} OF {fl.n}   ·   module {s.module}')
        self.title.set_text(_clip(s.name, 52))
        self.subtitle.set_text(f'{s.step_type}   ·   {r["process_area"]}')

        total = fl.cum['total'][-1] or 1.0
        share = _f(r['total_kgco2e']) / total * 100
        self.tiles[0].set_text(_fmt(r['total_kgco2e']))
        self.tiles[1].set_text(_fmt(fl.cum['total'][i]))
        self.tiles[2].set_text(f'{share:.2g}%' if share >= 0.01 else ('<0.01%' if share else '0%'))

        main, cuts = images
        self._show(self.ax3d, main, 'no image yet (make spx)')
        fit = main.with_name(main.name.replace('.3d.png', '.fit.png'))
        self._show(self.axfit, fit, '')
        for artist in (self.axfit[0], self.fit_label):
            artist.set_visible(fit.exists())
        for j, slot in enumerate(self.axcut):
            self._show(slot, cuts[j] if j < len(cuts) else None, 'no cut image (make spx)')
        self._mask(s)

        mats = fl.mats[i]
        gwps = ', '.join(f'{fl.mpw.props[m]["gwp"]:g}' for m, _ in mats if 'gwp' in fl.mpw.props.get(m, {}))
        inputs = ', '.join(f'{g} {_fmt(v, "g")}' for g, v in fl.supplied[i].items())
        values = {'process area': r['process_area'],
                  'material': _clip(', '.join(m for m, _ in mats), 22) or '—',
                  'thickness': _fmt(r['thickness_um'], 'µm') if r['thickness_um'] else '—',
                  'material GWP': f'{_clip(gwps, 18)} gCO2e/g' if gwps else '—',
                  'input gases': _clip(inputs, 26) or '—',
                  'energy': f'{_fmt(r["tool_energy_kwh"], "kWh")} + {_fmt(r["overhead_energy_kwh"])} overhead',
                  'EPW': _fmt(r['epw_kgco2e'], 'kgCO2e'),
                  'EPW tool': _fmt(r['tool_epw_kgco2e'], 'kgCO2e'),
                  'EPW overhead': _fmt(r['overhead_epw_kgco2e'], 'kgCO2e'),
                  'MPW': _fmt(r['mpw_kgco2e'], 'kgCO2e') + (f'  (gas {_fmt(r["mpw_gas_kgco2e"])})'
                                                            if _f(r['mpw_gas_kgco2e']) else ''),
                  'GPW': _fmt(r['gpw_kgco2e'], 'kgCO2e'),
                  'embodied carbon': _fmt(r['total_kgco2e'], 'kgCO2e')}
        for k, v in values.items():
            self.info[k].set_text(v)
        self.notes.set_text(_wrap('   ·   '.join(self.step_notes(idx, r, s)), 100, 3))

        for key, (past, now, caret, value, tot) in self.meters.items():
            before = fl.cum[key][i - 1] if i else 0.0
            inc = fl.cum[key][i] - before
            past.set_width(before)
            now.set_x(before)
            now.set_width(inc)
            caret.set_data(([before + inc], [1.5]) if inc else ([], []))
            value.set_text(f'{_fmt(fl.cum[key][i])} of {_fmt(tot)}' + (f'   (this step +{_fmt(inc)})' if inc else ''))

        self.line_past.set_data(range(1, i + 2), fl.cum['total'][:i + 1])
        self.dot.set_data([i + 1], [fl.cum['total'][i]])

        counts = defaultdict(float)
        for rr in fl.rows[:i + 1]:
            counts[rr['process_area']] += fl.count(rr['process_area'])
        mats_so_far = defaultdict(float)
        for d in fl.mats[:i + 1]:
            for m, g in d:
                mats_so_far[fl.material_key(m)] += g
        self._set_hbars(self.breakdowns[0], counts, {r['process_area']})
        self._set_hbars(self.breakdowns[1], mats_so_far, {fl.material_key(m) for m, _ in mats})
        self._set_hbars(self.breakdowns[2], _accumulate(fl.emitted[:i + 1], False), set(fl.emitted[i]))
        return self.fig

    def step_notes(self, idx, r, s):
        """The notes under this step's numbers: how it counts and ran in spx, the CSV comment."""
        fl = self.fl
        notes = []
        ran = fl.spx.get(idx, {})
        if r['process_area'] in fl.skip:
            notes.append(f'{r["process_area"]} step: not counted as a process step (uncounted_process_areas)')
        elif ran.get('spx') == 'custom_noop':
            notes.append('carbon-only step: no spx operation (structure unchanged)')
        elif ran.get('spx') == 'zero_filled':
            notes.append(f'spx ran with 0 values for: {", ".join(ran["zero_filled"])}')
        if r['notes']:
            notes.append(f'not counted: {r["notes"]}')
        if s.comment:
            notes.append(s.comment)
        return notes


# ---------------------------------------------------------------- front and summary pages

def _page_title(fig, title, subtitle=''):
    fig.text(0.05, 0.9, title, fontsize=22, fontweight='bold', color=INK, va='bottom')
    if subtitle:
        fig.text(0.05, 0.868, subtitle, fontsize=10, color=INK2, va='bottom')
    _hline(fig, 0.05, 0.95, 0.85, color=AXIS, lw=0.8)


def cover_page(plt, tech, manifest, totals, n_steps, generated, extra_lines=()):
    fig = _new_page(plt)
    fig.text(0.07, 0.82, tech.name, fontsize=34, fontweight='bold', color=INK)
    fig.text(0.07, 0.775, f'process design review   ·   {math.ceil(n_steps)} steps   ·   generated by CarbonEDA v{__version__} on '
             f'{generated}', fontsize=12, color=INK2)
    total = _f(totals['total_kgco2e'])
    fig.text(0.07, 0.56, f'{total:,.1f}', fontsize=60, fontweight='bold', color=INK)
    fig.text(0.07, 0.525, 'kgCO2e per wafer start', fontsize=12, color=INK2)

    # part to whole: one stacked bar with surface gaps between segments, stat tiles beneath
    ax = fig.add_axes([0.07, 0.445, 0.6, 0.03])
    ax.set_axis_off()
    left = 0.0
    for k, col, hatch in (('epw', 'tool_epw', None), ('epw', 'overhead_epw', OVERHEAD_HATCH),
                          ('mpw', 'mpw', None), ('gpw', 'gpw', None)):
        v = _f(totals[f'{col}_kgco2e'])
        ax.barh([0], [v], left=left, height=1, color=COLORS[k], edgecolor=SURFACE, lw=2, hatch=hatch)
        left += v
    ax.set_xlim(0, left or 1)
    for k, key in enumerate(('epw', 'mpw', 'gpw')):
        v = _f(totals[f'{key}_kgco2e'])
        x = 0.07 + k * 0.2
        fig.add_artist(plt.Rectangle((x, 0.355), 0.007, 0.04, color=COLORS[key], transform=fig.transFigure))
        fig.text(x + 0.016, 0.372, f'{v:,.1f}', fontsize=20, fontweight='bold', color=INK)
        fig.text(x + 0.016, 0.352, f'{LABELS[key]}   ·   {v / total * 100 if total else 0:.0f}%', fontsize=9,
                 color=INK2)

    lines = [f'wafer {_f(totals["wafer_diameter_mm"]):.0f} mm   ·   yield {_f(totals["yield"]):g}   ·   '
             f'carbon intensity {_f(totals["ci_fab_kgco2e_per_kwh"]):g} kgCO2e/kWh   ·   '
             f'facility overhead {_f(totals["facility_overhead_fraction"]):g} x tool energy',
             f'EPW {_f(totals["epw_kgco2e"]):,.1f} = tool {_f(totals["tool_epw_kgco2e"]):,.1f} + facility overhead '
             f'{_f(totals["overhead_epw_kgco2e"]):,.1f} kgCO2e '
             f'({_f(totals["tool_energy_kwh"]):,.0f} + {_f(totals["overhead_energy_kwh"]):,.0f} kWh)']
    if 'total_kgco2e_per_good_die' in totals:
        lines.append(f'die {_f(totals["die_area_mm2"]):g} mm²: {_f(totals["gross_dies_per_wafer"]):,.0f} '
                     f'gross dies per wafer, {_f(totals["total_kgco2e_per_good_die"]) * 1000:,.2f} gCO2e per good die')
    lines += extra_lines
    fig.text(0.07, 0.25, '\n'.join(lines), fontsize=10, color=INK2, va='top', linespacing=1.9)
    fig.text(0.07, 0.06, f'flow {rel(tech.csv)}\nlayout {rel(tech.layout)}\nstructure images from the spx run of '
             f'{manifest.get("generated", "?").replace("T", " ")}', fontsize=7.5, color=MUTED, va='bottom',
             linespacing=1.6)
    return fig


def parameters_pages(plt, cfg, fl):
    """The CarbonEDA assumptions behind every number in the report: one page, or two when the flow uses
    so many gas process areas that the GWP table and MPW no longer fit under them."""
    fig = _new_page(plt)
    figs = [fig]
    _page_title(fig, 'CarbonEDA parameters', 'config/carbon_config.yaml and materials.yaml as used for every '
                                             'number in this report')
    c, g = cfg.carbon, cfg.gpw
    used = defaultdict(int)
    for r in fl.rows:
        used[r['process_area']] += fl.count(r['process_area'])

    def section(fig, x, y, key, title, sub):
        fig.add_artist(plt.Rectangle((x, y - 0.002), 0.005, 0.026, color=COLORS[key], transform=fig.transFigure))
        fig.text(x + 0.012, y, title, fontsize=12, fontweight='bold', color=INK, va='bottom')
        fig.text(x, y - 0.03, sub, fontsize=8, color=INK2, va='bottom')

    def continued():
        if len(figs) == 1:
            figs.append(_new_page(plt))
            _page_title(figs[1], 'CarbonEDA parameters (continued)')
        return figs[1]

    section(fig, 0.05, 0.79, 'epw', 'EPW  energy', f'(tool + {overhead_fraction(c):g} x tool facility overhead) '
                                                   f'x {c["ci_fab_kgco2e_per_kwh"]} kgCO2e/kWh')
    # the process areas this flow uses (all of config/carbon_config.yaml's run into the MPW section)
    areas = {a: v for a, v in c['energy_per_step_kwh'].items() if used.get(a)} or c['energy_per_step_kwh']
    body = [[a, f'{float(v):.3g}', str(math.ceil(used.get(a, 0)))] for a, v in areas.items()]
    y_epw = _table(fig, 0.05, 0.745, [0.11, 0.07, 0.07], ['process area', 'kWh / step', 'steps'], body, size=8, row=0.03)

    d = g.get('defaults') or {}
    section(fig, 0.35, 0.79, 'gpw', 'GPW  process gases',
            f'kg x (1 - utilization) x (1 - abatement) x GWP, plus byproducts kg/kg x (1 - abatement) x GWP; '
            f'defaults: utilization {d.get("utilization", 1.0)}, abatement {d.get("abatement_rate", 0.0)}')
    gases = g.get('gases') or {}
    abate = lambda gas: (gases.get(gas) or {}).get('abatement_rate', d.get('abatement_rate', 0.0))
    body = []
    # the process areas this flow uses, as for EPW
    inputs = c.get('input_gases_per_step') or {}
    for area, ins in ({a: v for a, v in inputs.items() if used.get(a)} or inputs).items():
        for gas, u in (ins or {}).items():
            f = ((g.get(area) or {}).get(gas)) or {}
            byp = ', '.join(f'{b} {v:.2g}' for b, v in (f.get('byproducts') or {}).items())
            body.append([area, gas, f'{u["kg_gas"] * 1000:.3g}', f'{u["kgco2e_per_kg_gas"]:g}',
                         f'{f.get("utilization", d.get("utilization", 1.0)):g}', f'{abate(gas):g}', _clip(byp, 50)])
    y = _table(fig, 0.35, 0.745, [0.075, 0.045, 0.05, 0.06, 0.065, 0.06, 0.245],
               ['area', 'gas', 'g / step', 'upstream*', 'utilization', 'abatement', 'byproducts (kg per kg gas)'],
               body, size=7.5, row=0.03)
    fig.text(0.35, y - 0.01, '* kgCO2e per kg of gas made, counted in MPW', fontsize=7, color=MUTED, va='top')

    # GWP of every gas emitted, in two columns: under the input gases, or on the next page
    gw = [[gas, f'{v.get("gwp_kgco2e_per_kg", "")}', f'{abate(gas):g}'] for gas, v in gases.items()]
    half = (len(gw) + 1) // 2
    gwp_height = 0.02 + 0.028 * (half + 1)   # title gap, header and rows
    gx, gy, gfig = 0.35, y - 0.065, fig
    if gy - gwp_height < 0.04:
        gfig, gx, gy = continued(), 0.05, 0.79
    gfig.text(gx, gy, 'GWP (kgCO2e per kg, 100-yr) and abatement of every gas emitted', fontsize=8,
              color=INK2, va='bottom', fontweight='bold')
    for k, part in enumerate((gw[:half], gw[half:])):
        _table(gfig, gx + k * 0.3, gy - 0.01, [0.08, 0.08, 0.08], ['gas', 'GWP', 'abatement'], part, size=7.5,
               row=0.028)
    right = gy - gwp_height if gfig is fig else y   # the bottom of the GPW column on the first page

    # MPW: under EPW when the GPW column ends above it, otherwise on the next page
    y_mpw = min(0.3, y_epw - 0.05)
    mfig = fig
    if right < y_mpw + 0.06:
        mfig = continued()
        y_mpw = gy - gwp_height - 0.06 if gfig is mfig else 0.79
    wafer = (cfg.materials.get('Wafer') or {}).get('mpw_gco2e_per_300mm_wafer')
    section(mfig, 0.05, y_mpw, 'mpw', 'MPW  materials',
            f'blanket film over a {cfg.fab.wafer_diameter_mm:g} mm wafer: thickness x area x density x GWP'
            + (f'; the wafer itself {wafer:g} gCO2e (300 mm)' if wafer else '') + f'; yield {cfg.fab.yield_:g}')
    mats = [[m, f'{fl.mpw.props[m].get("density", 0):g}', f'{fl.mpw.props[m].get("gwp", 0):.4g}']
            for m in fl.material_names]
    per = max(3, int((y_mpw - 0.045 - 0.06) / 0.028) - 1)   # rows that fit above the footer
    for k in range(min(4, (len(mats) + per - 1) // per)):
        _table(mfig, 0.05 + k * 0.225, y_mpw - 0.045, [0.08, 0.055, 0.065], ['material', 'g/cm³', 'gCO2e/g'],
               mats[k * per:(k + 1) * per], size=7.5, row=0.028)
    if len(mats) > per * 4:
        mfig.text(0.05, 0.03, f'… and {len(mats) - per * 4} more', fontsize=7.5, color=MUTED)
    return figs


def summary_pages(plt, tech, rows, totals, spx_steps=(), count=lambda area: 1, skip=frozenset()):
    """skip: the uncounted areas (the wafer itself, bonds, flips): no step count. Modules of only those with no
    carbon (bonds, flips) are left out of the module chart; in the process-area table their carbon (the wafer)
    is a row with no step count."""
    figs = []
    segments = (('epw', 'tool_epw', None, 'EPW tool energy'),
                ('epw', 'overhead_epw', OVERHEAD_HATCH, 'EPW facility overhead'),
                ('mpw', 'mpw', None, 'MPW materials'), ('gpw', 'gpw', None, 'GPW gases'))
    by_mod = defaultdict(lambda: dict.fromkeys([c for _, c, _, _ in segments] + ['steps'], 0.0))
    order = []
    for r in rows:
        if r['module'] not in by_mod:
            order.append(r['module'])
        m = by_mod[r['module']]
        m['steps'] += count(r['process_area'])
        for _, col, _, _ in segments:
            m[col] += _f(r[f'{col}_kgco2e'])
    order = [m for m in order if by_mod[m]['steps'] or any(by_mod[m][c] for _, c, _, _ in segments)]
    fig = _new_page(plt)
    _page_title(fig, 'Carbon by module', 'kgCO2e per wafer, in flow order (steps in brackets)')
    ax = fig.add_axes([0.17, 0.06, 0.74, 0.74])
    _quiet(ax, keep=())
    ys = list(range(len(order)))[::-1]
    left = [0.0] * len(order)
    for k, col, hatch, label in segments:
        vals = [by_mod[m][col] for m in order]
        ax.barh(ys, vals, left=left, height=0.6, color=COLORS[k], label=label, hatch=hatch,
                edgecolor=SURFACE, linewidth=1.5)
        left = [a + b for a, b in zip(left, vals)]
    for y, tot in zip(ys, left):
        ax.text(tot, y, f'  {tot:,.1f}', va='center', fontsize=7.5, color=INK2)
    ax.set_yticks(ys)
    ax.set_yticklabels([m + (f'  ({math.ceil(by_mod[m]["steps"])})' if by_mod[m]['steps'] else '') for m in order],
                       fontsize=8 if len(order) < 30 else 6,
                       color=INK2)
    ax.grid(axis='x', color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    ax.legend(loc='lower right', bbox_to_anchor=(1, 1.0), ncol=4, frameon=False, fontsize=8.5)
    figs.append(fig)

    cols = ('tool_energy_kwh', 'overhead_energy_kwh', 'tool_epw_kgco2e', 'overhead_epw_kgco2e', 'epw_kgco2e',
            'mpw_kgco2e', 'gpw_kgco2e', 'total_kgco2e')
    by_area = defaultdict(lambda: [0] + [0.0] * len(cols))
    other = defaultdict(lambda: [0.0] * len(cols))   # uncounted areas: column sums, no step count
    for r in rows:
        if r['process_area'] in skip:
            other[r['process_area']] = [x + _f(r[k]) for x, k in zip(other[r['process_area']], cols)]
            continue
        a = by_area[r['process_area']]
        a[0] += count(r['process_area'])
        for i, k in enumerate(cols, 1):
            a[i] += _f(r[k])
    fig = _new_page(plt)
    _page_title(fig, 'Totals by process area', 'kgCO2e per wafer; EPW = tool + facility overhead '
                                               f'(overhead energy = tool energy x {_f(totals["facility_overhead_fraction"]):g})')
    head = ['process area', 'steps', 'tool kWh', 'overhead kWh', 'EPW tool', 'EPW overhead', 'EPW', 'MPW', 'GPW',
            'total']
    body = [[k, str(math.ceil(v[0]))] + [f'{x:,.2f}' for x in v[1:]] for k, v in sorted(by_area.items(), key=lambda kv: -kv[1][-1])]
    body += [[k, ''] + [f'{x:,.2f}' for x in v] for k, v in sorted(other.items(), key=lambda kv: -kv[1][-1]) if v[-1]]
    body.append(['all', str(math.ceil(sum(v[0] for v in by_area.values())))] + [f'{_f(totals[k]):,.2f}' for k in cols])
    _table(fig, 0.05, 0.8, [0.13, 0.055] + [0.083] * len(cols), head, body, size=8.5, row=0.036, bold_first=True)
    figs.append(fig)

    gaps = defaultdict(list)
    for r in rows:
        for note in filter(None, r['notes'].split('; ')):
            gaps[note].append(int(r['index']))
    # Zero-filled spx steps are geometry gaps unless their zero_fill type has ask: false (config/spx_steps.yaml)
    zero_fill = spxsteps.config() if spx_steps else None
    for s in spx_steps:
        if s.get('spx') == 'zero_filled' and spxsteps.asks(zero_fill, s['step_type']):
            gaps[f'spx: {s["step_type"]} run with 0 values (no geometry)'].append(s['index'])
    if gaps:
        fig = _new_page(plt)
        _page_title(fig, 'Config gaps', 'Steps without part of their carbon (add it in config/) or geometry '
                                        '(add the spx parameters to the flow CSV)')
        body = [[_clip(note, 60), str(len(idx)), _clip(_ranges(sorted(idx)), 110)] for note, idx in
                sorted(gaps.items(), key=lambda kv: -len(kv[1]))][:22]
        _table(fig, 0.05, 0.8, [0.3, 0.06, 0.54], ['missing', 'steps', 'step indices'], body, size=8, row=0.032)
        figs.append(fig)
    return figs


def _footer(fig, tech, generated):
    fig.text(0.95, 0.012, f'CarbonEDA v{__version__}  ·  {tech.name}  ·  generated {generated}', fontsize=6.5, color=MUTED,
             ha='right')


def run(tech, steps=None, extension=None):
    """extension(tech, cfg), if given, adds to the report:
    .suffix of the PDF name, .cover_lines(totals), .front_pages(plt, fl) and .step_page(plt, fl, *StepPage args)."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    from .render import cut_specs

    plt.rcParams.update({'font.family': FONT, 'pdf.compression': 9, 'axes.unicode_minus': False})
    manifest = json.loads(tech.manifest.read_text())
    flow = csvio.load_flow(tech.csv)
    all_rows = _read(tech.carbon_dir / 'steps.csv')
    rows = [r for r in all_rows if int(r['index']) > 0]   # index 0: carrier wafers, no step page
    totals = {r['metric']: r['value'] for r in _read(tech.carbon_dir / 'totals.csv')}
    gases = defaultdict(list)
    for g in _read(tech.carbon_dir / 'gases.csv'):
        gases[int(g['index'])].append(g)
    cfg = Config()
    fl = Flow(tech, flow, rows, gases, totals, cfg, [r for r in all_rows if int(r['index']) == 0],
              {s['index']: s for s in manifest['steps']})

    ext = extension(tech, cfg) if extension else None
    layout_error = ''
    try:
        layout = Layout(tech.layout, tech.cfg['clip']['coords'])
    except Exception as e:  # missing gdstk or an unreadable layout: the mask panel says so
        layout_error = str(e) or type(e).__name__
        print(f'WARNING: layout {rel(tech.layout)} not read ({layout_error}); no mask callouts')
        layout = None
    generated = datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')
    versions = {'csv': file_version(tech.csv), 'layout': file_version(tech.layout), 'tool': tool_version(),
                'generated': generated}

    tech.report_dir.mkdir(parents=True, exist_ok=True)
    out = tech.report_dir / f'{tech.name}_design_review{ext.suffix if ext else ""}.pdf'
    selected = [i for i, r in enumerate(rows) if not steps or steps[0] <= int(r['index']) <= steps[1]]
    with_img = 0
    with PdfPages(out) as pdf:
        front = [cover_page(plt, tech, manifest, totals, sum(fl.count(r['process_area']) for r in rows), generated,
                            ext.cover_lines(totals) if ext else ()),
                 *parameters_pages(plt, cfg, fl)] + (ext.front_pages(plt, fl) if ext else [])
        for fig in front:
            _footer(fig, tech, generated)
            pdf.savefig(fig, facecolor=SURFACE)
            plt.close(fig)
        args = (cut_specs(tech), layout, versions, layout_error)
        page = ext.step_page(plt, fl, *args) if ext else StepPage(plt, fl, *args)
        for n, i in enumerate(selected, 1):
            s = fl.by_index[int(rows[i]['index'])]
            main = tech.img_dir / f'{s.stem}.3d.png'
            cuts = sorted(tech.img_dir.glob(f'{s.stem}.cut*.png'))
            with_img += main.exists()
            pdf.savefig(page.update(i, (main, cuts)), facecolor=SURFACE)
            if n % 100 == 0:
                print(f'  {n}/{len(selected)} pages')
        plt.close(page.fig)
        summary = summary_pages(plt, tech, all_rows, totals, manifest['steps'], fl.count, fl.skip)
        for fig in summary:
            _footer(fig, tech, generated)
            pdf.savefig(fig, facecolor=SURFACE)
            plt.close(fig)
    print(f'{out}: {len(selected) + len(front) + len(summary)} pages ({len(selected)} steps, {with_img} with images)')
    return out
