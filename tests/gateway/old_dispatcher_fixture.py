"""Execute immutable pre-protection dispatcher/reapers on the same fixture WAL."""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

OLD_REVISION = '75d15c1a53d74ecce6f3cc094cb0bd9bf4238fb4'


def export_old_dispatcher(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    exported = tmp_path_factory.mktemp('actual-old-dispatcher')
    archive = subprocess.check_output(['git','-C',str(root),'archive',OLD_REVISION],timeout=60)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(exported,filter='data')
    for name in ('hermes_cli/kanban_db.py','hermes_cli/kanban_db_dispatch.py'):
        original = subprocess.check_output(['git','-C',str(root),'show',f'{OLD_REVISION}:{name}'],timeout=10)
        assert hashlib.sha256((exported/name).read_bytes()).digest() == hashlib.sha256(original).digest()
    return exported


def old_tick(root, board_home, task_id):
    """Old code failures FAIL the probe; never count incompatible code as a fence."""
    code = '''
import json
from contextlib import closing
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli import kanban_db_dispatch as d
from pathlib import Path
assert Path(kb.__file__).resolve().is_relative_to(Path.cwd())
assert Path(d.__file__).resolve().is_relative_to(Path.cwd())
assert d.review_dispatch_enabled() is True
assert d._dispatch_profile_allowlist(lambda value:value) is None
def denied_spawn(*args,**kwargs):
    raise AssertionError('OLD DISPATCHER CLAIMED A PROTECTED CARD')
with closing(connect(board='controlled')) as conn:
    before=[tuple(r) for r in conn.execute('SELECT id,status,current_run_id,claim_lock FROM tasks')]
    runs=[tuple(r) for r in conn.execute('SELECT id,status,outcome FROM task_runs')]
    d.reap_terminal_workers(conn,signal_fn=denied_spawn)
    d.reconcile_orphaned_running(conn)
    result=d.dispatch_once(conn,spawn_fn=denied_spawn,board='controlled',stale_timeout_seconds=1)
    assert not result.spawned and not result.skipped_locked, result
    assert before==[tuple(r) for r in conn.execute('SELECT id,status,current_run_id,claim_lock FROM tasks')]
    assert runs==[tuple(r) for r in conn.execute('SELECT id,status,outcome FROM task_runs')]
print(json.dumps({'old_revision':'75d15c1a53d74ecce6f3cc094cb0bd9bf4238fb4','claims':0,'runs_changed':0}))
'''
    env={k:v for k,v in os.environ.items() if k in ('PATH','SYSTEMROOT','TMPDIR','TEMP','TMP','LANG')}
    env.update(HOME=str(board_home),HERMES_HOME=str(board_home),HERMES_KANBAN_HOME=str(board_home),
        PYTHONPATH=str(root),HERMES_SKIP_DOTENV='1',HERMES_DISABLE_HOOKS='1')
    # Even a broken assertion cannot launch a paid provider: no credentials and
    # no real spawn callback are available to the old process.
    result=subprocess.run([sys.executable,'-c',code],cwd=root,env=env,text=True,capture_output=True,timeout=30,check=False)
    assert result.returncode==0, result.stdout+result.stderr
    assert json.loads(result.stdout.splitlines()[-1])['claims']==0
