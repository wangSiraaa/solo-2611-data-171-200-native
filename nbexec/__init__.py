"""Local notebook executor built directly on jupyter_client and nbformat.

The package deliberately does not wrap nbclient's whole-notebook
``execute``: every execute request, channel message and output mutation is
handled here so that message ownership (parent header), display semantics
(update_display_data / deferred clear_output) and the run record stay
fully inspectable.
"""

from .runner import NotebookRunResult, run_notebook
from .parameters import apply_parameters, ParameterOverrideError

__all__ = [
    "NotebookRunResult",
    "run_notebook",
    "apply_parameters",
    "ParameterOverrideError",
]
