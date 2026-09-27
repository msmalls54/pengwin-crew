"""Web run caps are durable and atomic; tests never execute queued work."""

import os
import subprocess
import sys


def _run_script(tmp_path, name: str, script: str) -> None:
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / name}",
        "PAYMENT_MODE": "simulated",
        "PLANNER_MODE": "vultr",
        "SANDBOX_MODE": "docker",
    })
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_web_code_limit_is_exact_and_survives_demo_reset(tmp_path):
    _run_script(tmp_path, "web-code-limit.db", """
from sqlalchemy import func, select
from crew.db import AgentJob, ControlFlag, CrewRun, SessionLocal
from crew.seed import seed_demo
from crew.jobs import submit_web_code_run

seed_demo(reset=True)
ids = [submit_web_code_run(f'Print {index}') for index in range(30)]
assert len(set(ids)) == 30
try:
    submit_web_code_run('One too many')
except ValueError as exc:
    assert 'limit reached' in str(exc)
else:
    raise AssertionError('31st run escaped the cap')
with SessionLocal() as session:
    assert session.get(ControlFlag, 'web_code_runs').value == '30'
    assert session.scalar(select(func.count()).select_from(CrewRun).where(
        CrewRun.source_user == 'web-demo')) == 30
    assert session.scalar(select(func.count()).select_from(AgentJob)) == 30
seed_demo(reset=True)
with SessionLocal() as session:
    assert session.get(ControlFlag, 'web_code_runs').value == '30'
    assert session.scalar(select(func.count()).select_from(CrewRun)) == 0
try:
    submit_web_code_run('Still over the cap')
except ValueError:
    pass
else:
    raise AssertionError('Reset improperly replenished the cap')
""")


def test_legacy_run_counts_are_captured_before_reset(tmp_path):
    _run_script(tmp_path, "web-upgrade.db", """
from uuid import uuid4
from sqlalchemy import func, select
from crew.db import ControlFlag, CrewRun, SessionLocal, init_db
from crew.seed import seed_demo
from crew.jobs import submit_web_code_run, submit_web_demo_run

init_db()
with SessionLocal.begin() as session:
    for source, total in (('web-demo', 29), ('web-admin', 30)):
        for _ in range(total):
            session.add(CrewRun(id=str(uuid4()), flow='code-task', source_user=source,
                                channel_id='web', status='COMPLETE'))
    assert session.get(ControlFlag, 'web_code_runs') is None
seed_demo(reset=True)  # An upgrade may reset old runs; keep their lifetime usage.
with SessionLocal() as session:
    assert session.get(ControlFlag, 'web_code_runs').value == '29'
    assert session.get(ControlFlag, 'web_admin_runs').value == '30'
    assert session.scalar(select(func.count()).select_from(CrewRun)) == 0
submit_web_code_run('The final allowed code run')
try:
    submit_web_demo_run('berlin-pantry')
except ValueError as exc:
    assert 'limit reached' in str(exc)
else:
    raise AssertionError('Legacy admin run count was forgotten')
with SessionLocal() as session:
    assert session.get(ControlFlag, 'web_code_runs').value == '30'
    assert session.get(ControlFlag, 'web_admin_runs').value == '30'
""")


def test_last_web_slot_is_atomic_under_concurrent_submitters(tmp_path):
    _run_script(tmp_path, "web-concurrent.db", """
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from sqlalchemy import func, select
from crew.db import ControlFlag, CrewRun, SessionLocal
from crew.seed import seed_demo
from crew.jobs import submit_web_code_run, submit_web_demo_run

seed_demo(reset=True)
with SessionLocal.begin() as session:
    session.get(ControlFlag, 'web_code_runs').value = '29'
    session.get(ControlFlag, 'web_admin_runs').value = '29'

def race(submit):
    gate = Barrier(3)
    def one():
        gate.wait()
        try:
            return ('accepted', submit())
        except ValueError as exc:
            assert 'limit reached' in str(exc)
            return ('limited', None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(one), pool.submit(one)
        gate.wait()
        result = [first.result(timeout=15), second.result(timeout=15)]
    assert sorted(item[0] for item in result) == ['accepted', 'limited'], result

race(lambda: submit_web_code_run('One final run'))
race(lambda: submit_web_demo_run('berlin-pantry'))
with SessionLocal() as session:
    assert session.get(ControlFlag, 'web_code_runs').value == '30'
    assert session.get(ControlFlag, 'web_admin_runs').value == '30'
    assert session.scalar(select(func.count()).select_from(CrewRun).where(
        CrewRun.source_user == 'web-demo')) == 1
    assert session.scalar(select(func.count()).select_from(CrewRun).where(
        CrewRun.source_user == 'web-admin')) == 1
""")


def test_failed_queue_rolls_back_reserved_web_slot(tmp_path):
    _run_script(tmp_path, "web-rollback.db", """
from sqlalchemy import func, select
from crew.db import ControlFlag, CrewRun, SessionLocal
from crew.seed import seed_demo
import crew.jobs as jobs

seed_demo(reset=True)
original = jobs._new_job
jobs._new_job = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('queue failed'))
try:
    jobs.submit_web_code_run('No job can be queued')
except RuntimeError:
    pass
else:
    raise AssertionError('Expected queue failure')
jobs._new_job = original
with SessionLocal() as session:
    assert session.get(ControlFlag, 'web_code_runs').value == '0'
    assert session.scalar(select(func.count()).select_from(CrewRun)) == 0
assert jobs.submit_web_code_run('Now queue it')
with SessionLocal() as session:
    assert session.get(ControlFlag, 'web_code_runs').value == '1'
""")
