# Author: David Kong
# License: MIT

"""outputs/<tech>/progress.log: one timestamped line per stage event, plus live step
counts (with rate and ETA) while spx and svisual run.

    tail -f outputs/<tech>/progress.log
"""

import contextlib
import datetime
import threading
import time


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _dur(sec):
    sec = int(sec)
    return f'{sec // 3600}h{sec % 3600 // 60:02d}m' if sec >= 3600 else f'{sec // 60}m{sec % 60:02d}s'


class Progress:
    def __init__(self, tech):
        self.path = tech.outputs / 'progress.log'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, msg):
        line = f'{_now()}  {msg}'
        with self._lock:
            print(line, flush=True)
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')

    @contextlib.contextmanager
    def watch(self, stage, directory, pattern, total, every=30):
        """Logs how many files matching pattern were written in directory since entry.
        x and x.zst count once (zstd keeps the mtime)."""
        start = time.time()
        stop = threading.Event()

        def count():
            names = set()
            for p in directory.glob(pattern):
                with contextlib.suppress(FileNotFoundError):  # removed by zstd meanwhile
                    if p.stat().st_mtime >= start - 1:
                        names.add(p.name.removesuffix('.zst'))
            return len(names)

        def loop():
            last = -1
            while not stop.wait(every):
                n = count()
                if n == last:
                    continue
                last = n
                el = time.time() - start
                eta = f', ETA {_dur(el / n * (total - n))}' if 0 < n < total else ''
                self.log(f'{stage}: {n}/{total} steps ({n / total * 100:.0f}%), elapsed {_dur(el)}{eta}')

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join()
            self.log(f'{stage}: {count()}/{total} steps written in {_dur(time.time() - start)}')
