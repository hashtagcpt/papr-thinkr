#!/usr/bin/env python3
"""papr-thinkr launcher -- the one entry point.

    python papr.py serve                       # web UI at http://127.0.0.1:8765
    python papr.py review paper.docx [...]     # review from the command line
    python papr.py --help                      # everything else

Same as `python -m papr_thinkr ...`. The code lives in papr_thinkr/.
"""
from papr_thinkr.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
