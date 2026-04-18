"""Entry point for `python -m code_mapper`.

Guards `main()` under `__name__ == "__main__"` so importing this module
doesn't execute the CLI as a side effect. Propagates the return value
through sys.exit so future versions of main() can signal non-zero exit
for CI integration (e.g., --fail-on severity).
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main() or 0)
