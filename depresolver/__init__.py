"""Local wheel dependency resolver.

Core rules:
  * the wheelhouse is the only package source (no index, no source dists);
  * only ``py3-none-any`` pure-Python wheels are supported;
  * markers are evaluated against the *target* environment, never the host;
  * name/version identity is verified against METADATA, WHEEL and RECORD;
  * version specs/extras use packaging's declaration semantics.
"""

__version__ = "1.0"
