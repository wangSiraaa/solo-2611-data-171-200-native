"""Explicit accounting of live uncompressed data buffers.

The executor is forbidden from materialising the whole array, so the budget
is checked structurally rather than with RSS.  A ``BufferLedger`` records
every *unique* block of uncompressed numerical storage that is live at once.

Rules implemented here:

* every block has a stable key; acquiring a live key is an error, so a block
  can never be double counted;
* a *view* (a numpy array sharing another block's storage, or a ``bytes``
  object viewed as an array) is registered against its owner and charged
  zero additional bytes;
* compressed input/output byte strings live in a separate counter and are
  never part of the data-buffer budget.

``peak`` is the maximum sum of charged block sizes observed at any acquire.
"""

from .errors import BudgetExceeded


class Ledger:
    def __init__(self, budget, name="data"):
        self.budget = budget
        self.name = name
        self._live = {}          # key -> dict(label, nbytes, owner)
        self._peak = 0
        self._peak_live = ()
        self.events = []         # (op, key, label, nbytes, total_after)
        self.charged_total = 0   # cumulative bytes ever charged
        self.peak_stage = None
        self.stage = None

    # -- lifecycle -------------------------------------------------------
    def set_stage(self, stage):
        self.stage = stage

    def acquire(self, key, label, nbytes, owner=None):
        """Charge ``nbytes`` for a new unique block.

        ``owner`` marks this block as a view of an already-charged block; then
        ``nbytes`` must be 0.
        """
        if key in self._live:
            raise AssertionError(f"buffer {key!r} is already live")
        if nbytes < 0:
            raise AssertionError("negative buffer size")
        if owner is not None and nbytes != 0:
            raise AssertionError("a shared view must not be charged")
        if owner is not None and owner not in self._live:
            raise AssertionError(f"view {key!r} refers to unknown owner {owner!r}")
        self._live[key] = {"label": label, "nbytes": nbytes, "owner": owner}
        self.charged_total += nbytes
        total = self.current
        if total > self.budget:
            del self._live[key]
            self.charged_total -= nbytes
            raise BudgetExceeded(
                f"{self.name} buffer budget exceeded: {total} > {self.budget} "
                f"while acquiring {label} ({nbytes} bytes)"
            )
        self.events.append(("acquire", key, label, nbytes, total))
        if total > self._peak:
            self._peak = total
            self._peak_live = tuple(
                (k, v["label"], v["nbytes"]) for k, v in self._live.items()
            )
            self.peak_stage = self.stage
        return key

    def release(self, key):
        if key not in self._live:
            raise AssertionError(f"buffer {key!r} is not live")
        # Child views must be released before their owner.
        for k, v in self._live.items():
            if v["owner"] == key:
                raise AssertionError(f"buffer {k!r} still views {key!r}")
        nbytes = self._live.pop(key)["nbytes"]
        self.events.append(("release", key, "", nbytes, self.current))

    def release_if_live(self, key):
        if key in self._live:
            self.release(key)

    @property
    def current(self):
        return sum(v["nbytes"] for v in self._live.values())

    @property
    def peak(self):
        return self._peak

    @property
    def live_count(self):
        return len(self._live)

    def report(self):
        return {
            "budget_bytes": self.budget,
            "peak_bytes": self._peak,
            "peak_stage": self.peak_stage,
            "peak_live_blocks": [
                {"key": k, "label": l, "nbytes": n} for k, l, n in self._peak_live
            ],
            "charged_total_bytes": self.charged_total,
            "live_at_end": [
                {"key": k, "label": v["label"], "nbytes": v["nbytes"]}
                for k, v in self._live.items()
            ],
            "event_count": len(self.events),
        }


class ByteCounter:
    """Separate, non-budgeted accounting for compressed / metadata bytes."""

    def __init__(self, name):
        self.name = name
        self.input_bytes = 0
        self.output_bytes = 0
        self.count = 0

    def add_input(self, n):
        self.input_bytes += n
        self.count += 1

    def add_output(self, n):
        self.output_bytes += n
        self.count += 1

    def report(self):
        return {
            "input_compressed_bytes": self.input_bytes,
            "output_compressed_bytes": self.output_bytes,
            "operations": self.count,
            "note": "compressed/metadata bytes are excluded from the data-buffer budget",
        }


class InterpreterOverhead:
    """Records the interpreter/process baseline, kept out of the budget."""

    def __init__(self):
        self.baseline = None

    def capture(self):
        # No psutil dependency; report the notion explicitly instead of RSS.
        import sys

        self.baseline = {
            "python": sys.version.split()[0],
            "note": (
                "Interpreter, numpy internals and compressed byte strings are not "
                "part of data_buffer_budget_bytes; only unique live uncompressed "
                "numeric buffers are charged. RSS is not used as a measurement."
            ),
        }

    def report(self):
        return self.baseline or {}
