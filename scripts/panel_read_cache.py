"""Small process-local caches for verified read-only projections, never permits.

Callers must recheck source access, identity and owner bindings for every key.
Only JSON metadata is retained; errors and oversized values are not cached.
"""
from collections import OrderedDict
import json
import threading
import time


class ReadCache:
    def __init__(self, *, ttl=5, entries=4, max_bytes=8 * 1024 * 1024, max_wait=10, clock=time.monotonic):
        self.ttl, self.entries, self.max_bytes, self.clock = ttl, entries, max_bytes, clock
        self.max_wait = max_wait
        self._condition = threading.Condition()
        self._values = OrderedDict()
        self._building = False

    def get(self, key, build):
        # One builder per cache bounds concurrent decompression and allocation.
        # Other cache instances and cheap endpoints remain independent.
        deadline = time.monotonic() + self.max_wait
        with self._condition:
            while True:
                cached = self._values.get(key)
                if cached and 0 <= self.clock() - cached[0] <= self.ttl:
                    self._values.move_to_end(key)
                    return json.loads(cached[1])
                if not self._building:
                    self._building = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('projection_busy')
                self._condition.wait(remaining)
        try:
            value = build()
            parts, size = [], 0
            for chunk in json.JSONEncoder(ensure_ascii=True, allow_nan=False, separators=(',', ':')).iterencode(value):
                raw = chunk.encode('ascii'); size += len(raw)
                if size > self.max_bytes:
                    return value
                parts.append(raw)
            encoded = b''.join(parts)
            with self._condition:
                self._values[key] = (self.clock(), encoded)
                self._values.move_to_end(key)
                while len(self._values) > self.entries:
                    self._values.popitem(last=False)
            return json.loads(encoded)
        finally:
            with self._condition:
                self._building = False
                self._condition.notify_all()
