"""Bounded diagnostic policy/failure tests; only generated isolated profiles."""
from contextlib import contextmanager
import errno
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from services import diagnostic_policy as policy
from services.process_launch import quiet_options


def child(tmp_path, code):
    temporary=tmp_path/'tmp';temporary.mkdir(exist_ok=True)
    environment=dict(os.environ,KEIVOTOS_HOME=str(tmp_path/'profile'),TMPDIR=str(temporary),
                     TMP=str(temporary),TEMP=str(temporary),PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
    environment['PYTHONPATH']=str(ROOT/'backend')
    return subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=environment,text=True,capture_output=True)


def session(root, *, limits=None):
    identity=uuid4().hex
    path=root/identity/'events-0001.jsonl'
    owner=policy.LogSession(root,identity,[path],limits)
    return owner,path


def test_pure_services_do_not_load_config_or_create_profile(tmp_path):
    result=child(tmp_path,"import sys; import services.log_bootstrap, services.diagnostic_policy, services.process_launch; assert 'config' not in sys.modules; print('ok')")
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='ok' and not (tmp_path/'profile').exists()


@pytest.mark.parametrize('value',[{'retention_days':True},{'total_mib':15},{'rotation_mib':65},
                                  {'retention_days':0},{'rotation_mib':32,'total_mib':16}])
def test_policy_rejects_invalid_limits(value):
    with pytest.raises(ValueError):
        policy.policy(value)


def test_cleanup_only_new_owned_expired_logs(tmp_path):
    root=tmp_path/'logs'
    old,path=session(root);path.write_bytes(b'owned old diagnostic')
    previous=time.time()-15*86400;os.utime(path,(previous,previous));old.close()
    legacy=root/'keivotos-runtime-historical.log';legacy.write_bytes(b'preserved legacy')
    note=root/'notes.md';note.write_bytes(b'preserved authored note')
    current,active=session(root)
    active.write_bytes(b'active')
    try:
        result=current.cleanup()
        assert result['removed_files']==1 and not path.exists()
        assert active.read_bytes()==b'active'
        assert legacy.read_bytes()==b'preserved legacy' and note.read_bytes()==b'preserved authored note'
    finally:current.close()


def test_total_budget_evicts_oldest_inactive_logs_first(tmp_path):
    root=tmp_path/'logs';limits={'total_mib':16,'rotation_mib':1}
    first,a=session(root,limits=limits);second,b=session(root,limits=limits)
    for path,age in ((a,7200),(b,3600)):
        with path.open('wb') as output:output.truncate(9*policy.MIB)
        when=time.time()-age;os.utime(path,(when,when))
    first.close();second.close()
    current,_=session(root,limits=limits)
    try:
        result=current.cleanup()
        assert not a.exists() and b.exists()
        assert result['removed_bytes']==9*policy.MIB and result['managed_bytes']==9*policy.MIB
    finally:current.close()


def test_active_writer_is_protected_and_budget_refuses_more_disk_output(tmp_path):
    root=tmp_path/'logs';limits={'total_mib':16,'rotation_mib':1}
    owner,path=session(root,limits=limits)
    with path.open('wb') as output:output.truncate(17*policy.MIB)
    expired=time.time()-100*86400;os.utime(path,(expired,expired))
    current,_=session(root,limits=limits)
    try:
        result=current.cleanup()
        assert path.exists() and result['budget_blocked']
        assert not current.allow(1)
    finally:owner.close();current.close()


@pytest.mark.parametrize('kind',['escape','symlink','hardlink'])
def test_cleanup_refuses_links_and_escaping_markers(tmp_path,kind):
    root=tmp_path/'logs';outside=tmp_path/'user.sqlite';outside.write_bytes(b'irreplaceable fixture')
    owner,path=session(root)
    if kind=='escape':
        (owner.directory/'managed.json').write_text(json.dumps({'version':1,'files':['../user.sqlite']}))
    else:
        try:
            if kind=='symlink':path.symlink_to(outside)
            else:os.link(outside,path)
        except OSError:
            owner.close();pytest.skip('Native link privilege unavailable')
    owner.close();current,_=session(root)
    try:
        result=current.cleanup(now=time.time()+100*86400)
        assert outside.read_bytes()==b'irreplaceable fixture'
        assert result['removed_files']==0 and result['protected_or_invalid']>=1
    finally:current.close()


def test_debug_session_expires_on_wall_or_monotonic_deadline(monkeypatch):
    wall=[1000.0];monotonic=[50.0]
    monkeypatch.setattr(policy.time,'time',lambda:wall[0])
    monkeypatch.setattr(policy.time,'monotonic',lambda:monotonic[0])
    try:
        assert policy.debug_session(True)['remaining_seconds']==1800
        wall[0]+=1801
        assert not policy.debug_session()['active']
        policy.debug_session(True);monotonic[0]+=1801;wall[0]-=1000
        assert not policy.debug_session()['active']
        assert not policy.debug_session(False)['active']
    finally:policy.debug_session(False)


def test_runtime_primary_unavailable_uses_safe_fallback_and_valid_json(tmp_path):
    result=child(tmp_path,r"""
import json,logging
from unittest.mock import patch
import runtime_logging as runtime
original=runtime.LogSession
def create(root,*args,**kwargs):
    if root==runtime.LOG_DIR:raise PermissionError('password=private-primary')
    return original(root,*args,**kwargs)
with patch.object(runtime,'LogSession',side_effect=create):
    paths=runtime.configure_runtime_logging()
    logging.getLogger('keivotos.fixture').warning('password=private-record',extra={'event_code':'fixture.saved'})
    state=runtime.diagnostics_state()
    logging.shutdown()
    print(json.dumps({'paths':[str(path) for path in paths],'status':state}))
""")
    assert result.returncode==0,result.stderr
    state=json.loads(result.stdout)
    assert state['status']['fallback'] and state['status']['buffered']==0
    assert 'primary_log_directory_unavailable' in result.stderr
    files=list((tmp_path/'tmp').rglob('*.jsonl'))
    assert files and all(json.loads(line)['event_code'] for p in files for line in p.read_text(encoding='utf-8').splitlines())
    assert 'private-record' not in ''.join(p.read_text(encoding='utf-8') for p in (tmp_path/'tmp').rglob('*.log'))
    assert not (tmp_path/'profile/user.sqlite').exists()


def test_full_disk_buffers_bounded_safe_records_and_recovers_without_stopping_work(tmp_path):
    result=child(tmp_path,r"""
import errno,json,logging
from unittest.mock import patch
import runtime_logging as runtime
runtime.configure_runtime_logging()
writers=[h for h in runtime._owned_handlers if isinstance(h,runtime._Resilience)]
patches=[]
for writer in writers:
    patches.extend([patch.object(writer,'guard_write',return_value=False),
                    patch.object(writer,'_fallback_write',side_effect=OSError(errno.ENOSPC,'password=private-failure'))])
    if isinstance(writer,runtime.DiagnosticEventHandler):
        patches.append(patch.object(writer,'write_guard',return_value=False))
for change in patches:change.start()
for number in range(320):
    logging.getLogger('keivotos.fixture').warning('password=private-buffer %s %s',number,'x'*6000)
before=runtime.diagnostics_state()
assert all(len(h.buffer)<=256 and h.buffer_bytes<=1024*1024 for h in writers)
assert all('private-buffer' not in line for h in writers for line in h.buffer)
for change in reversed(patches):change.stop()
logging.getLogger('keivotos.fixture').warning('recovered work continues')
after=runtime.diagnostics_state()
logging.shutdown()
print(json.dumps({'before':before,'after':after}))
""")
    assert result.returncode==0,result.stderr[-2000:]
    value=json.loads(result.stdout)
    assert value['before']['buffered']>0 and value['before']['dropped']>0
    assert value['after']['buffered']==0 and value['after']['recovered']>0
    assert 'private-failure' not in result.stderr and 'private-buffer' not in result.stderr
    assert 'storage_unavailable_buffered' in result.stderr and 'buffer_recovered' in result.stderr


def test_early_crash_has_safe_persisted_path_without_loading_profile(tmp_path):
    result=child(tmp_path,"from services.log_bootstrap import install; install(); raise RuntimeError('password=private-crash')")
    assert result.returncode!=0 and 'startup_failure; log:' in result.stderr
    files=list((tmp_path/'tmp').rglob('startup.log'))
    assert len(files)==1
    saved=files[0].read_text(encoding='utf-8')
    assert 'startup.unhandled_exception' in saved and 'RuntimeError' in saved
    assert 'private-crash' not in saved+result.stderr
    assert not (tmp_path/'profile').exists()


def test_rotation_limits_and_machine_readable_stdout_are_independent_of_diagnostics(tmp_path):
    result=child(tmp_path,r"""
import json,logging
import runtime_logging as runtime
runtime.configure_runtime_logging()
writers=[h for h in runtime._owned_handlers if isinstance(h,runtime._Resilience)]
assert all(h.maxBytes==16*1024*1024 and h.backupCount==2 for h in writers)
logging.getLogger('keivotos.fixture').info('finished',extra={'event_code':'operation.phase_finished',
    'event_details':{'duration_ms':12,'processed':4,'success':3,'skipped':1,'failed':0}})
logging.shutdown()
print(json.dumps({'result':'kept'}))
""")
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'result':'kept'}
    assert 'processed=4' in result.stderr and 'skipped=1' in result.stderr
    assert '\r' not in result.stderr and '\x1b' not in result.stderr


def test_legacy_verbose_preference_is_independent_of_temporary_debug(tmp_path):
    result=child(tmp_path,r"""
import json,logging
from unittest.mock import patch
import config,runtime_logging as runtime
runtime.configure_runtime_logging()
config._cfg['verbose_logging']=True
runtime.set_debug_session(True)
logging.getLogger('keivotos.fixture').debug('within debug session')
with patch('services.diagnostic_policy.time.monotonic',return_value=10**20):
    assert not runtime.debug_status()['active']
logging.getLogger('keivotos.fixture').debug('expired debug session')
assert config.get_diagnostics_preferences()['verbose_logging'] is True
logging.shutdown()
print(json.dumps({'ok':True}))
""")
    assert result.returncode==0,result.stderr
    assert 'within debug session' in result.stderr and 'expired debug session' not in result.stderr
    assert json.loads(result.stdout)=={'ok':True}


def test_quiet_windows_options_preserve_process_group_and_linux_arguments(monkeypatch):
    import services.process_launch as launch
    monkeypatch.setattr(launch.sys,'platform','win32')
    monkeypatch.setattr(launch.subprocess,'CREATE_NO_WINDOW',0x08000000,raising=False)
    values={'creationflags':0x200,'cwd':'generated'}
    assert launch.quiet_options(values)=={'creationflags':0x08000200,'cwd':'generated'}
    assert values['creationflags']==0x200
    monkeypatch.setattr(launch.sys,'platform','linux')
    assert launch.quiet_options(values)==values and launch.quiet_options()=={}


@pytest.mark.skipif(sys.platform!='win32',reason='Native Windows console API')
def test_native_helper_has_no_console_and_retains_stdout(tmp_path):
    result=subprocess.run([sys.executable,'-c',"import ctypes,json; print(json.dumps({'console':int(ctypes.windll.kernel32.GetConsoleWindow())}))"],
                          capture_output=True,text=True,**quiet_options())
    assert result.returncode==0 and json.loads(result.stdout)=={'console':0}


def test_additive_policy_models_reject_bodies_paths_and_wrong_types():
    from routers.diagnostics import LogLimits,DebugRequest
    from pydantic import ValidationError
    for value in ({'retention_days':True},{'path':'outside'},{'rotation_mib':0},{'total_mib':'256'}):
        with pytest.raises(ValidationError):LogLimits(**value)
    with pytest.raises(ValidationError):DebugRequest(active='yes')
    assert DebugRequest(active=True).active


def test_policy_persists_without_changing_old_preferences_or_debug_restart(tmp_path):
    first=child(tmp_path,r'''
import json
import config
from routers.diagnostics import LogLimits,DebugRequest,update_log_policy,change_debug_session
config.save_config({'verbose_logging':True})
result=update_log_policy(LogLimits(retention_days=21,total_mib=64,rotation_mib=2))
assert change_debug_session(DebugRequest(active=True))['debug']['active']
print(json.dumps(result['limits']))
''')
    assert first.returncode==0,first.stderr
    assert json.loads(first.stdout)=={'retention_days':21,'total_mib':64,'rotation_mib':2}
    second=child(tmp_path,r'''
import json,config
from routers.diagnostics import log_policy
result=log_policy()
assert config.get_diagnostics_preferences()['verbose_logging']
assert not result['debug']['active']
print(json.dumps(result['limits']))
''')
    assert second.returncode==0,second.stderr
    assert json.loads(first.stdout)==json.loads(second.stdout)


def test_early_unwritable_fallback_has_bounded_redacted_memory(monkeypatch):
    from services import log_bootstrap as boot
    monkeypatch.setattr(boot,'_writer',None)
    monkeypatch.setattr(boot,'_session',None)
    monkeypatch.setattr(boot,'_early',__import__('collections').deque())
    monkeypatch.setattr(boot,'_early_bytes',0)
    monkeypatch.setattr(boot,'_dropped',0)
    def unavailable():raise PermissionError('password=private-storage')
    monkeypatch.setattr(boot,'fallback_root',unavailable)
    for number in range(300):
        assert boot.record_failure('fixture.failure',ValueError('password=private-early')) is None
    assert len(boot._early)<=256 and boot._early_bytes<=1024*1024
    assert boot.state()['startup_dropped']>0
    assert 'private-early' not in ''.join(boot._early)


@pytest.mark.skipif(sys.platform=='win32',reason='POSIX PTY; native Windows console/redirect tests run separately')
def test_real_tty_uses_readable_lines_without_corrupting_stdout(tmp_path):
    import pty
    master,slave=pty.openpty()
    temporary=tmp_path/'tmp';temporary.mkdir()
    environment=dict(os.environ,KEIVOTOS_HOME=str(tmp_path/'profile'),TMPDIR=str(temporary),
                     PYTHONPATH=str(ROOT/'backend'),PYTHONDONTWRITEBYTECODE='1')
    code="import logging,json; from runtime_logging import configure_runtime_logging; configure_runtime_logging(); logging.getLogger('keivotos.fixture').info('terminal action complete'); print(json.dumps({'result':True}))"
    try:
        result=subprocess.run([sys.executable,'-c',code],env=environment,cwd=ROOT,
                              stdout=subprocess.PIPE,stderr=slave,text=True,timeout=15)
        os.close(slave);slave=None
        chunks=[]
        while True:
            try:
                part=os.read(master,4096)
                if not part:break
                chunks.append(part)
            except OSError:break
        output=b''.join(chunks).decode('utf-8','replace')
        assert result.returncode==0 and json.loads(result.stdout)=={'result':True}
        assert ' INFO keivotos.fixture: terminal action complete' in output
        assert chr(27) not in output
    finally:
        if slave is not None:os.close(slave)
        os.close(master)


def test_live_policy_change_preserves_concurrent_writer_events(tmp_path):
    result=child(tmp_path,r'''
import json,logging
from concurrent.futures import ThreadPoolExecutor
import config,runtime_logging as runtime
runtime.configure_runtime_logging()
def write(index):
    logging.getLogger('keivotos.fixture').info('event %s',index,
        extra={'event_code':'fixture.concurrent','event_details':{'index':index}})
with ThreadPoolExecutor(max_workers=6) as pool:
    futures=[pool.submit(write,index) for index in range(100)]
    for number in range(4):
        config.save_config({'diagnostic_limits':{'retention_days':14+number,'total_mib':256,'rotation_mib':2}})
        runtime.apply_log_policy()
    for future in futures:future.result()
logging.shutdown()
from pathlib import Path
rows=[json.loads(line) for path in config.DIAGNOSTIC_LOG_FILE.parent.glob('*.jsonl*') for line in path.read_text(encoding='utf-8').splitlines()]
assert sorted(row['details']['index'] for row in rows if row['event_code']=='fixture.concurrent')==list(range(100))
print(json.dumps({'events':100}))
''')
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'events':100}


def test_fallback_reservation_fits_inside_combined_profile_budget(tmp_path):
    result=child(tmp_path,r'''
import json
import runtime_logging as runtime
runtime.configure_runtime_logging()
primary=next(handler.session for handler in runtime._owned_handlers if isinstance(handler,runtime._Resilience))
fallback=runtime._get_fallback_session()
assert primary.budget_bytes+fallback.budget_bytes==256*1024*1024
assert fallback.budget_bytes==16*1024*1024
print(json.dumps({'within_budget':True}))
''')
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'within_budget':True}


def test_early_crash_budget_buffers_then_recovers_without_growing_full_store(tmp_path):
    result=child(tmp_path,r'''
import json
from pathlib import Path
from unittest.mock import patch
from services import log_bootstrap as boot
path=Path(boot.record_failure('fixture.before_full'))
before=path.stat().st_size
with patch.object(boot._session,'allow',return_value=False):
    assert boot.record_failure('fixture.buffered_budget') is None
    assert path.stat().st_size==before and len(boot._early)==1
assert boot.record_failure('fixture.after_budget')==str(path)
assert not boot._early
saved=path.read_text(encoding='utf-8')
assert 'fixture.buffered_budget' in saved and 'fixture.after_budget' in saved
print(json.dumps({'recovered':True}))
''')
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'recovered':True}
