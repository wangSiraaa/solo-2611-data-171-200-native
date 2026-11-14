"""bsimp -- domain-wide boundary simplification for planar GeoJSON partitions.

The package builds one unified planar graph (nodes / atomic edges / arcs /
face-side relations) for a set of adjacent polygons, simplifies each shared
arc exactly once, and fits the single resulting geometry back into every
region.  Outer domain boundary and protected holes are never modified.
"""

__version__ = "1.0.0"
