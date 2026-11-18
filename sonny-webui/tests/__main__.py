"""Entry point so `py -3 sonny-webui\\tests` works on Windows without -m discover.

Equivalent to::

    py -3 -m unittest discover -s sonny-webui\\tests

Running the directory directly sidesteps the ``unittest`` discovery quirk where
a start directory containing a hyphen anywhere in its path (for example
``sonny-webui``) can be rejected with
``ImportError: Start directory is not importable``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(str(HERE), top_level_dir=str(HERE))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
