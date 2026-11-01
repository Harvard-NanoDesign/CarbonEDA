# Author: David Kong
# License: MIT

"""zstd for the per-step tdr files (~0.5 GB each from spx, ~90x smaller compressed).

sim compresses each <stem>.tdr to <stem>.tdr.zst as soon as spx has moved on to the next
step; the original is removed only once the .zst passes `zstd -t`, and zstd keeps the mtime.
vis and sprocess-check decompress one at a time. By hand, for svisual/sprocess:
    zstd -d -k outputs/<tech>/tdr/<stem>.tdr.zst
Level: tdr_zstd_level in config/tools.yaml (0 keeps plain .tdr files).
"""

import contextlib
import shutil
import subprocess
import threading

from .paths import tools_config

ZST = '.zst'


def level():
    return int(tools_config().get('tdr_zstd_level', 10) or 0)


def zstd():
    exe = shutil.which('zstd')
    if not exe:
        raise SystemExit('zstd not found on PATH (needed for compressed tdr files)')
    return exe


def find(tdr):
    """tdr (.../<stem>.tdr) as spx wrote it, else its .tdr.zst, else None."""
    if tdr.exists():
        return tdr
    z = tdr.with_name(tdr.name + ZST)
    return z if z.exists() else None


def compress(tdr, lvl):
    """tdr -> tdr.zst; removes tdr once the .zst tests OK. Returns True on success."""
    z = tdr.with_name(tdr.name + ZST)
    ok = (subprocess.call([zstd(), f'-{lvl}', '-qq', '-f', str(tdr), '-o', str(z)]) == 0
          and subprocess.call([zstd(), '-t', '-qq', str(z)]) == 0)
    if ok:
        tdr.unlink()
    return ok


def decompress(src, dst):
    """src (.tdr.zst) -> dst (.tdr); raises CalledProcessError on failure."""
    subprocess.check_call([zstd(), '-d', '-qq', '-f', str(src), '-o', str(dst)])


@contextlib.contextmanager
def while_written(tdr_dir, progress, every=30):
    """While spx runs: every `every` s, compress each *.tdr in tdr_dir but the newest (which spx
    may still be writing). On exit (spx done or failed), compress whatever is left."""
    lvl = level()
    if not lvl:
        yield
        return
    zstd()
    stop = threading.Event()
    failed = set()

    def sweep(keep_newest):
        tdrs = sorted(tdr_dir.glob('*.tdr'), key=lambda p: p.stat().st_mtime)
        for p in tdrs[:-1] if keep_newest else tdrs:
            if p not in failed and not compress(p, lvl):
                failed.add(p)
                progress.log(f'sim: zstd failed on {p.name}; left uncompressed')

    def loop():
        while not stop.wait(every):
            sweep(keep_newest=True)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()
        sweep(keep_newest=False)
        progress.log(f'sim: tdr compressed with zstd -{lvl} as written'
                     + (f'; {len(failed)} left uncompressed' if failed else ''))
