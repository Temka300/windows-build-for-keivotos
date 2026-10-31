"""Control only an explicitly recorded download child, never the application."""
import os
import time
import psutil
from services.operations import process_identity


def alive(pid,birth):
    if type(pid) is not int or pid<=1 or pid==os.getpid():return False
    return bool(birth and process_identity(pid)==birth)


def _process(pid,birth):
    if not alive(pid,birth):raise ValueError('Download worker identity changed')
    process=psutil.Process(pid)
    if not process.is_running():raise ValueError('Download worker exited')
    return process


def _snapshot(process):
    return {'pid':process.pid,'created':process.create_time()}


def _matching(record):
    try:
        process=psutil.Process(record['pid'])
        if process.pid==os.getpid() or process.create_time()!=record['created'] or not process.is_running():
            return None
        return process
    except psutil.NoSuchProcess:return None


def resume(records):
    """Only processes this controller suspended, with creation identity intact."""
    for record in reversed(records):
        process=_matching(record)
        if process is not None:
            try:process.resume()
            except psutil.NoSuchProcess:pass


def suspend(pid,birth):
    root=_process(pid,birth)
    changed=[];seen=set()
    try:
        # Stop the root first so it cannot create another child while enumerating.
        pending=[root]
        for _ in range(4):
            for process in pending:
                if process.pid in seen:continue
                seen.add(process.pid)
                try:
                    if process.status()==psutil.STATUS_STOPPED:continue
                    snapshot=_snapshot(process)
                    process.suspend()
                    changed.append(snapshot)
                except psutil.NoSuchProcess:continue
            pending=[p for p in root.children(recursive=True) if p.pid not in seen]
            if not pending:return changed
        raise RuntimeError('Download process tree did not become stable')
    except BaseException:
        resume(changed)
        raise


def stop(pid,birth,timeout=8):
    """Force-drain this verified child tree before any unfinished-file removal."""
    root=_process(pid,birth)
    # Freeze the root before enumerating so drain cannot miss a newly spawned child.
    root.suspend()
    children=root.children(recursive=True)
    processes=[*reversed(children),root]
    for process in processes:
        try:
            if process.is_running():process.kill()
        except psutil.NoSuchProcess:pass
    deadline=time.monotonic()+timeout
    while True:
        live=[]
        for process in processes:
            try:
                if process.is_running() and process.status()!=psutil.STATUS_ZOMBIE:live.append(process)
            except psutil.NoSuchProcess:pass
        if not live:return
        if time.monotonic()>=deadline:raise RuntimeError('Download worker did not stop')
        time.sleep(.02)


def stop_records(records,timeout=8):
    """Drain saved descendants even if their original root has already exited."""
    processes=[]
    for record in records:
        process=_matching(record)
        if process is not None:
            try:process.kill();processes.append(process)
            except psutil.NoSuchProcess:pass
    deadline=time.monotonic()+timeout
    while processes:
        remaining=[]
        for process in processes:
            try:
                if process.is_running() and process.status()!=psutil.STATUS_ZOMBIE:remaining.append(process)
            except psutil.NoSuchProcess:pass
        if not remaining:return
        if time.monotonic()>=deadline:raise RuntimeError('Paused descendants did not stop')
        processes=remaining;time.sleep(.02)
