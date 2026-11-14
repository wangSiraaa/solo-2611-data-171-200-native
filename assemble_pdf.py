#!/usr/bin/env python3
"""CLI entry point: python assemble_pdf.py --selection ... --output ... --report ..."""
from pdfpack.assemble import main

if __name__ == "__main__":
    raise SystemExit(main())
