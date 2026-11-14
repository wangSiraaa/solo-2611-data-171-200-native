"""pdfpack - assemble a new PDF package from selected pages of multiple sources.

The package contains two tools:

* :mod:`pdfpack.assemble` - validate a selection, clone selected pages (each at
  most once, sources untouched), retarget internal link annotations, named
  destinations and the outline to output pages, and emit a combined PDF plus a
  full mapping/diagnostic report.
* :mod:`pdfpack.verify`   - independently re-open the produced PDF and verify
  every jump reference, the names tree, the outline hierarchy and page
  geometry/contents against the sources and the report.

Only ordinary internal GoTo links (explicit destinations and named
destinations) and existing named destinations / outline items are in scope.
See SCOPE.md for the exact boundary.
"""

__all__ = ["assemble", "verify"]
