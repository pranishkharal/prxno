"""Run the complete infrastructure test-suite with the stdlib runner.

Usage:
    python tests/run_all.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


if __name__ == "__main__":
    tests_dir = Path(__file__).resolve().parent
    project_root = tests_dir.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    suite = unittest.TestLoader().discover(
        str(tests_dir), pattern="test_*.py", top_level_dir=str(project_root)
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)