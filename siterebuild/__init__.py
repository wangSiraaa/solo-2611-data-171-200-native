"""Offline rebuild of a small archived site from a real WARC + cutoff plan.

Entries: ``rebuild`` (export static site + provenance + gap reports),
``serve`` (local HTTP on 127.0.0.1) and ``verify`` (closure/digest/fragment
checks without a browser).  No network access, no JavaScript execution.
"""

__version__ = "1.0.0"
