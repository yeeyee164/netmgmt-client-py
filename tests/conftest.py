# -*- encoding: utf-8 -*-
"""
tests/conftest.py

Root test configuration and fixtures.
Ensures repository root is always on sys.path and exposes common test paths.
"""
import sys
import os
from pathlib import Path

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TESTS_ROOT = REPO_ROOT / "tests"
FIXTURES_DIR = TESTS_ROOT / "fixtures"
YANG_DIR = TESTS_ROOT / "yang"

