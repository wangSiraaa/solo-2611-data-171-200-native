"""A small, dependency-light Zarr v2 transpose/rechunk tool.

Public entry points:

* ``rechunk_tool.plan``  -- canonical plan validation and overlap planning
* ``rechunk_tool.executor`` -- budgeted, spool-based conversion
* ``rechunk_tool.verifier`` -- independent, full-read verification
"""

__version__ = "1.0.0"
