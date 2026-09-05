"""Entry point for ``python -m dp_backup``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
