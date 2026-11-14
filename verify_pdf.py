#!/usr/bin/env python3
"""CLI entry point: python verify_pdf.py --combined ... --report ... --fixtures-dir ..."""
from pdfpack.verify import main

if __name__ == "__main__":
    raise SystemExit(main())
