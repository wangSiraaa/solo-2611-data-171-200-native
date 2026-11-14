"""Diagnostic codes and the structured error used by pdfpack.

Every fatal condition discovered *before* assembly is represented by one of
these codes so that eligibility is reported as a complete set of problems
rather than inferred from how many pages happened to copy.
"""

# selection.json structure / schema problems
E_SELECTION_PARSE = "selection_parse_error"
E_SELECTION_SCHEMA = "selection_schema_error"
E_UNKNOWN_SOURCE = "unknown_source"
E_SOURCE_UNREADABLE = "source_unreadable"
E_SOURCE_ENCRYPTED = "encrypted_pdf_unsupported"
E_PAGE_OUT_OF_RANGE = "page_out_of_range"
E_DUPLICATE_SELECTION = "duplicate_source_page"

# unsupported document structures (the affected source is refused)
E_ACROFORM = "acroform_unsupported"
E_UNSUPPORTED_ACTION = "unsupported_action"
E_UNSUPPORTED_ANNOTATION = "unsupported_annotation"
E_NAMED_DEST_UNRESOLVABLE = "named_destination_unresolvable"
E_BROKEN_DESTINATION = "broken_destination"
E_UNSUPPORTED_NAMES_TREE = "unsupported_names_tree"
E_DOCUMENT_LEVEL_UNSUPPORTED = "document_level_feature_unsupported"

# non-fatal diagnostic recorded for dangling jumps
D_TARGET_OMITTED = "target_omitted"


class AssemblyError(Exception):
    """Raised when preflight finds one or more fatal diagnostics.

    ``diagnostics`` holds the full list of code/context dictionaries; the CLI
    serializes them instead of printing a single message.
    """

    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__(f"assembly refused: {len(diagnostics)} diagnostic(s)")
