# Author: David Kong
# License: MIT

"""The technology's layout (GDS or OASIS) inside its clip window, for the report's mask callout."""

import re

OASIS_MAGIC = b'%SEMI-OASIS\r\n'


def layer_names(step):
    """[(layer, datatype)] a litho step exposes, from its LayerNames ('401:0', '1:0|2:0', ...)."""
    value = step.value('LayerNames') or []
    out = []
    for name in value if isinstance(value, (list, tuple)) else [value]:
        m = re.fullmatch(r'\s*(\d+)\s*:\s*(\d+)\s*', str(name))
        if m:
            out.append((int(m.group(1)), int(m.group(2))))
    return out


def layout_format(path):
    """'OASIS' or 'GDSII', from the file's header rather than its extension (.oas, .oasis, .gds, .gdsii, ...)."""
    with open(path, 'rb') as f:
        return 'OASIS' if f.read(len(OASIS_MAGIC)) == OASIS_MAGIC else 'GDSII'


class Layout:
    def __init__(self, path, clip):
        """path: GDSII or OASIS file; clip: [x0, y0, x1, y1] in um."""
        try:
            import gdstk
        except ImportError:
            raise RuntimeError('gdstk is not installed (make venv)') from None
        self.gdstk = gdstk
        self.format = layout_format(path)
        read = gdstk.read_oas if self.format == 'OASIS' else gdstk.read_gds
        lib = read(str(path), unit=1e-6)
        self.cells = lib.top_level()
        if not self.cells:
            raise ValueError(f'no cells in {path} (read as {self.format})')
        self.clip = [float(c) for c in clip]
        self.window = gdstk.rectangle(self.clip[:2], self.clip[2:])
        self._cache = {}

    def polygons(self, layer, datatype):
        """Polygons ([(x, y), ...] in um) of one layer, cut to the clip window."""
        key = (layer, datatype)
        if key not in self._cache:
            polys = [p for c in self.cells for p in c.get_polygons(layer=layer, datatype=datatype)]
            clipped = self.gdstk.boolean(polys, self.window, 'and') if polys else []
            self._cache[key] = [p.points for p in clipped]
        return self._cache[key]
