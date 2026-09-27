"""Keep the public project record separate from the technical sandbox demo."""

import os
import subprocess
import sys


def test_public_activity_and_sandbox_routes_are_separate(tmp_path):
    # Importing crew.app in this process would pin settings before other tests
    # set their isolated database and payment mode.
    env = os.environ.copy()
    env.update({"DATABASE_URL": f"sqlite:///{tmp_path / 'site-routes.db'}"})
    script = r'''
from fastapi.testclient import TestClient
from crew.app import app

client = TestClient(app)
main = client.get('/')
assert main.status_code == 200
assert 'id="project-memory"' in main.text
assert 'id="judge-timeline"' in main.text
assert 'id="judge-spend"' in main.text
assert 'class="role-grid"' not in main.text
assert 'id="judge-containment"' not in main.text
assert 'id="code-goal"' not in main.text

sandbox = client.get('/sandbox')
assert sandbox.status_code == 200
assert 'id="judge-containment"' in sandbox.text
assert 'id="code-goal"' in sandbox.text
assert 'id="run-code"' in sandbox.text
assert 'id="project-memory"' not in sandbox.text
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
