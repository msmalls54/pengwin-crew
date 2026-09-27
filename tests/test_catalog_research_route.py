"""Run conversation route tests in a fresh process to keep settings test-local."""

import os
import subprocess
import sys
from pathlib import Path


def test_catalog_research_route_cases(tmp_path):
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{tmp_path / 'catalog-route.db'}"
    cases = Path(__file__).with_name("catalog_research_route_cases.py")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(cases)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
