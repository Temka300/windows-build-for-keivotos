"""Real synthetic download trees; no application/media processes are touched."""
from pathlib import Path
import os
import subprocess
import sys
import time
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from services import process_control
from services.operations import process_identity


@pytest.fixture
def child(tmp_path):
    leaf="import sys,time;from pathlib import Path;p=Path(sys.argv[1]);\nwhile True:\n with p.open('a') as f:f.write('.')\n time.sleep(.02)\n"
    script="""import subprocess,sys,time,os
from pathlib import Path
root=Path(sys.argv[1])
leaf=subprocess.Popen([sys.executable,'-c',sys.argv[2],str(root/'leaf.tick')],**({'creationflags':0x08000000} if sys.platform=='win32' else {}))
(root/'leaf.pid').write_text(str(leaf.pid))
while True:
 with (root/'root.tick').open('a') as f:f.write('.')
 time.sleep(.02)
"""
    options={'creationflags':0x08000000} if sys.platform=='win32' else {'start_new_session':True}
    process=subprocess.Popen([sys.executable,'-I','-c',script,str(tmp_path),leaf],**options)
    birth=process_identity(process.pid)
    try:
        for _ in range(250):
            if (tmp_path/'leaf.tick').exists() and (tmp_path/'root.tick').exists():break
            assert process.poll() is None
            time.sleep(.02)
        assert (tmp_path/'leaf.tick').exists()
        yield process,birth,tmp_path
    finally:
        if process_control.alive(process.pid,birth):process_control.stop(process.pid,birth)
        process.wait(timeout=10)


def sizes(root):
    return [(root/name).stat().st_size for name in ('root.tick','leaf.tick')]


def test_pause_and_resume_stop_both_real_writer_processes(child):
    process,birth,root=child
    records=process_control.suspend(process.pid,birth)
    try:
        before=sizes(root);time.sleep(.18)
        assert sizes(root)==before and len(records)>=2
        assert {process.pid,int((root/'leaf.pid').read_text())}<={record['pid'] for record in records}
    finally:process_control.resume(records)
    for _ in range(200):
        if all(a>b for a,b in zip(sizes(root),before)):break
        time.sleep(.02)
    assert all(a>b for a,b in zip(sizes(root),before))


def test_wrong_creation_identity_cannot_pause_or_stop(child):
    process,birth,root=child
    before=sizes(root)
    for action in (process_control.suspend,process_control.stop):
        with pytest.raises(ValueError):action(process.pid,birth+'-different')
    time.sleep(.1)
    assert all(a>b for a,b in zip(sizes(root),before))


def test_resume_does_not_thaw_a_reused_process_id(child):
    process,birth,root=child
    records=process_control.suspend(process.pid,birth)
    try:
        before=sizes(root)
        process_control.resume([dict(row,created=row['created']-100) for row in records])
        time.sleep(.1);assert sizes(root)==before
    finally:process_control.resume(records)


def test_application_process_is_never_a_download_target():
    assert not process_control.alive(os.getpid(),process_identity(os.getpid()))
    with pytest.raises(ValueError):process_control.suspend(os.getpid(),process_identity(os.getpid()))


def test_saved_descendants_are_drained_when_the_original_root_exits(child):
    process,birth,root=child
    records=process_control.suspend(process.pid,birth)
    process.kill();process.wait(timeout=5)
    before=sizes(root)
    process_control.stop_records(records)
    time.sleep(.1)
    assert sizes(root)==before
    for record in records:
        matching=process_control._matching(record)
        assert matching is None or matching.status()==process_control.psutil.STATUS_ZOMBIE
