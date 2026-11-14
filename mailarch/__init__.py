"""mailarch — offline EML archive organizer.

Reads a directory of .eml files and produces a normalized JSON manifest,
exported resources and a static read-only HTML index.  Pure standard
library; never connects to a mailbox and never sends mail.
"""

__version__ = "1.0.0"
