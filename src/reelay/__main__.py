"""Allow ``python -m reelay``."""

from __future__ import annotations

import sys

from reelay.cli import main

if __name__ == "__main__":
    sys.exit(main())
