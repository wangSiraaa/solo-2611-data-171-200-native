"""RDF dataset 规范化与判等。

公开 API:
    parse_nquads(text, source)  -> Dataset
    canonicalize(dataset)       -> CanonResult
    compare(ds_a, ds_b)         -> CompareResult
"""

from .model import Dataset, Quad  # noqa: F401
from .nqparser import parse_nquads, parse_nquads_file  # noqa: F401
from .canon import canonicalize, CanonResult  # noqa: F401
from .compare import compare, CompareResult  # noqa: F401
