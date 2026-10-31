"""One supervised downloader process; durable records, create-only publication."""
from services.operation_diagnostics import observed
import asyncio
from collections import deque
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import uuid

import config
from database import get_user_db
from files_base import sources, index, annotations, hashing, serving
from maintenance import exclusive_tool_operation
from modules.youtube.engine import canonical_url, dependencies, format_selector
from modules.youtube import metadata, channel_art, layout, saved_item, operational_state
from services import operations
from services import operation_diagnostics as diagnostics
from services.tool_diagnostics import ToolEvidence, command_details
from services.process_launch import quiet_options

logger=logging.getLogger('keivotos.youtube')
_pending=deque()
_lock=threading.RLock()


def get_jobs_db():
    return operational_state.connect(get_user_db)


def update(job_id, **values):
    allowed={'status','progress','title','path','thumbnail','error','failure_phase',*metadata.COLUMNS,*layout.COLUMNS}
    if not values or not set(values)<=allowed:raise ValueError('Invalid job fields')
    with _lock, get_jobs_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM youtube_downloads WHERE id=?',(job_id,)).fetchone()
        if row is None:raise ValueError('Download not found.')
        previous=dict(row)
        changed=any(key in values and values[key]!=previous.get(key) for key in ("status","phase","failure_phase"))
        operational_state.update(db,dict(row),values)
        db.execute('UPDATE youtube_downloads SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),job_id))
        state=diagnostics.snapshot(db,"youtube",job_id) if changed else None
        db.commit()

    if state is not None:
        diagnostics.committed("youtube", job_id, state, previous=previous["status"])


def get(job_id):
    with get_jobs_db() as db:
        row=db.execute('SELECT * FROM youtube_downloads WHERE id=?',(job_id,)).fetchone()
    if row:return dict(row)
    with get_user_db() as db:
        return operational_state.saved_job(db,job_id)


def destination(source_id, expected_root=None):
    with get_user_db() as db:source=sources.get_source(db,source_id)
    if source is None or not source.visible or source.role not in ('files','video','youtube'):
        raise ValueError('Choose a visible Files, Video or YouTube folder.')
    root=serving.resolve_within_source(source.path,'',[config.SUITE_HOME])
    if not root.is_dir() or (expected_root is not None and str(root)!=expected_root):
        raise ValueError('The destination folder changed or is unavailable.')
    return source,root


def preferences():
    with get_user_db() as db:
        row=db.execute('SELECT * FROM youtube_preferences WHERE id=1').fetchone()
        mode=row['mode'] if row else 'default'
        custom=row['custom_source'] if row else ''
        available=sources.list_sources(db,visible_only=True)
        default=next((s for s in available if s.role=='youtube'),None)
        selected=next((s for s in available if s.source_id==custom),None) if mode=='custom' else default
    return {'mode':mode,'path':selected.path if selected else '',
            'source_id':selected.source_id if selected else '',
            'default_path':default.path if default else ''}


def save_preferences(mode, path=None):
    if mode not in ('default','custom'):raise ValueError('Unknown save location mode.')
    custom=None
    if path is not None:
        reason=sources.unsafe_source_reason(path,[config.SUITE_HOME])
        if reason:raise ValueError(reason)
        with get_user_db() as db:
            existing=next((s for s in sources.list_sources(db) if Path(s.path).resolve()==Path(path).expanduser().resolve()),None)
            if existing and (not existing.visible or existing.role not in ('files','video','youtube')):
                raise ValueError('Choose a visible Files, Video or YouTube folder.')
            custom=(existing or sources.register_source(db,path)).source_id
    with get_user_db() as db:
        db.execute("INSERT OR IGNORE INTO youtube_preferences(id) VALUES(1)")
        db.execute('UPDATE youtube_preferences SET mode=? WHERE id=1',(mode,))
        if custom is not None:db.execute('UPDATE youtube_preferences SET custom_source=? WHERE id=1',(custom,))
        db.commit()
    return preferences()


def create(url,source_id=None,video='best',audio='best',request_id=None):
    url=canonical_url(url);format_selector(video,audio)
    request_id=str(uuid.UUID(str(request_id))) if request_id is not None else None
    with _lock, exclusive_tool_operation('queuing a YouTube download',wait=False), get_jobs_db() as db:
        if request_id is not None:
            acknowledged=operational_state.request_ack(get_user_db,request_id,url,source_id,video,audio)
            receipt=operations._one(db,'SELECT id FROM kv_job_requests WHERE owner=? AND request_key=?',('youtube',request_id))
            if receipt:
                from services.job_store import read_request
                request=read_request(db,owner='youtube',request_id=receipt['id'])
                payload=request['payload']
                if any(payload.get(k)!=v for k,v in dict(url=url,video=video,audio=audio).items()) or source_id is not None and payload['source_id']!=source_id:
                    raise ValueError('This request token belongs to another download.')
                operation=operations._one(db,'SELECT id FROM kv_operations WHERE owner=? AND request_id=?',('youtube',receipt['id']))
                if not operation:raise ValueError('Request acknowledgement is unavailable.')
                return get(operation['id'])
            if acknowledged is not None:return acknowledged
        if source_id is None:
            source_id=preferences()['source_id']
            if not source_id:raise ValueError('Choose a YouTube folder in Files, or a custom save location in YouTube settings.')
        _,root=destination(source_id)
        tools=dependencies()
        if not tools['ready']:raise ValueError('Missing: '+', '.join(tools['missing']))
        db.execute('BEGIN IMMEDIATE')
        job_id=enqueue(db,dict(url=url,source_id=source_id,root=str(root),video=video,audio=audio),request_key=request_id)
        db.commit()
        _pending.append(job_id)
    logger.info('Download queued: job=%s url=%s destination=%s',job_id,url,root,
                extra={'event_code':'operation.requested','operation_owner':'youtube','operation_id':job_id,
                       'event_details':{'state':'requested','parent_operation_id':diagnostics.current_context().get('operation_id')}})
    return get(job_id)


def enqueue(db,values,request_key=None):
    """Insert only; caller commits links and appends to the queue together."""
    if db.execute("SELECT 1 FROM youtube_downloads WHERE url=? AND source_id=? AND video=? AND audio=? AND status IN ('queued','downloading','merging')",
                  tuple(values[k] for k in ('url','source_id','video','audio'))).fetchone():
        raise ValueError('This download is already running.')
    if db.execute("SELECT count(*) AS total FROM youtube_downloads WHERE status='queued'").fetchone()['total']>=20:raise ValueError('The download queue is full.')
    job_id=uuid.uuid4().hex
    columns=['id',*values]
    db.execute('INSERT INTO youtube_downloads('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',
               (job_id,*values.values()))
    operational_state.accept(db,{**values,'id':job_id,'status':'queued'},key=request_key or job_id)
    if values.get('retry_of'):
        operations.transition(db,'youtube',job_id,phase='retry-pending')
    return job_id


def published_output(job,root):
    """Resolve recorded output, or one unambiguous legacy published media file.

    This never inspects staging or moves files. A retained output with missing,
    ambiguous or unsafe evidence is an error, rather than permission to download
    another copy. Legacy recovery is limited to Files' known inline media types.
    """
    owners={job['id']};ancestor=job
    while ancestor.get('retry_of'):
        ancestor=get(ancestor['retry_of'])
        if not ancestor or ancestor['id'] in owners or any(ancestor[k]!=job[k] for k in ('url','source_id','root','video','audio')):
            raise ValueError('The retry history changed. See Logs.')
        owners.add(ancestor['id'])
    if job.get('layout_version'):
        if job['layout_version']!=1:raise ValueError('Unsupported saved layout.')
        output=saved_item.read(root,job,owners)
        if output:
            folder=(root/output['path']).parent
            with get_user_db() as db:other_roots=[Path(s.path).resolve() for s in sources.list_sources(db) if s.source_id!=job['source_id']]
            if any(other.is_relative_to(root) and folder.is_relative_to(other) for other in other_roots):
                raise ValueError('Choose the nested destination source directly.')
        return output
    relative=job['path']
    parts=Path(relative).parts if relative else ('YouTube',job['id'])
    if relative and (len(parts)!=3 or parts[0]!='YouTube' or parts[1] not in owners):
        raise ValueError('The saved output location changed.')
    folder=root/'YouTube'/parts[1]
    if (root/'YouTube').is_symlink() or folder.is_symlink():
        raise ValueError('Published output contains a symbolic link.')
    if not folder.exists() and not relative:return None
    folder=serving.resolve_within_source(root,folder.relative_to(root).as_posix(),[config.SUITE_HOME])
    if not folder.is_dir():raise ValueError('Published output is unavailable.')
    with get_user_db() as db:other_roots=[Path(s.path).resolve() for s in sources.list_sources(db) if s.source_id!=job['source_id']]
    if any(other.is_relative_to(root) and folder.is_relative_to(other) for other in other_roots):
        raise ValueError('Choose the nested destination source directly.')
    if relative:
        path=folder/parts[2]
    else:
        candidates=[p for p in folder.iterdir() if p.is_file() and serving.inline_media_type(p)[0].startswith(('video/','audio/'))]
        if len(candidates)!=1:raise ValueError('Published output is ambiguous or unsupported. See Logs; files were preserved.')
        path=candidates[0]
    if path.is_symlink():raise ValueError('Published output contains a symbolic link.')
    path=serving.resolve_served_file(root,path.relative_to(root).as_posix(),[config.SUITE_HOME])
    if not path.stat().st_size:raise ValueError('Published output is empty. Files were preserved.')
    thumbnail=job['thumbnail']
    if not relative:
        covers=sorted(p for p in folder.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in ('.jpg','.jpeg','.png','.webp'))
        thumbnail=covers[0].relative_to(root).as_posix() if len(covers)==1 else ''
    if thumbnail:
        cover=root/thumbnail
        if cover.parent!=folder or cover.is_symlink():raise ValueError('Saved cover location changed.')
        # Missing artwork is optional; the completed media remains recoverable.
        if not cover.is_file():thumbnail=''
    return dict(path=path.relative_to(root).as_posix(),thumbnail=thumbnail,title=job['title'] or path.stem)


def _retire_catalog_retries(links, catalog_database):
    """Acknowledge durable operational acceptance in an explicit catalog only."""
    if catalog_database is None:return
    from modules.youtube.user_schema import retire_catalog_save
    try:
        for previous,replacement in links:
            if (replacement['retry_of']!=previous['id'] or previous['superseded_by']!=replacement['id']
                    or any(previous[key]!=replacement[key] for key in ('url','source_id','root','video','audio'))):
                raise ValueError('The accepted retry history changed.')
        with catalog_database() as db:
            if db.in_transaction:raise ValueError('Open a clean catalog retry connection.')
            db.execute('BEGIN IMMEDIATE')
            try:
                for previous,replacement in links:retire_catalog_save(db,previous['id'],replacement)
                db.commit()
            except BaseException:
                db.rollback()
                raise
    except Exception as error:
        raise ValueError('Retry accepted; catalog registration is incomplete. Repeat this retry request to reconcile it.') from error


def retry(job_id, *, catalog_database=None):
    catalog_database=catalog_database or operational_state.catalog(get_user_db)
    with _lock:
        item=get(job_id);seen=set();links=[]
        if not item:raise ValueError('Download not found.')
        # Repeated requests for the same old card return its current replacement,
        # including after that replacement finished or was itself retried.
        while item['superseded_by']:
            if item['id'] in seen:raise ValueError('The retry history changed. See Logs.')
            seen.add(item['id']);previous=item;item=get(item['superseded_by'])
            if not item:raise ValueError('The retry history changed. See Logs.')
            links.append((previous,item))
        if seen:
            _retire_catalog_retries(links,catalog_database)
            if item['status']=='queued':
                with get_jobs_db() as ready_db:
                    operations.transition(ready_db,'youtube',item['id'],phase='queued')
            if catalog_database is not None and item['status']=='queued' and item['id'] not in _pending:
                if len(_pending)>=20:raise ValueError('Retry accepted; the queue is full. Repeat this retry request later.')
                _pending.append(item['id'])
            return item
        if item['status'] not in ('failed','cancelled','interrupted'):raise ValueError('This download cannot be retried.')
        _,root=destination(item['source_id'],item['root'])
        output=published_output(item,root)
        if output is None:
            tools=dependencies()
            if not tools['ready']:raise ValueError('Missing: '+', '.join(tools['missing']))
        values={k:item[k] for k in ('url','source_id','root','video','audio','title')}
        values.update(retry_of=item['id'])
        if output:
            values.update(output,progress=95,failure_phase='integration')
            values.update({name:item[name] for name in metadata.COLUMNS})
            values.update({name:item[name] for name in layout.COLUMNS})
        with get_jobs_db() as db:
            db.execute('BEGIN IMMEDIATE')
            replacement=enqueue(db,values)
            db.execute('UPDATE youtube_downloads SET superseded_by=? WHERE id=?',(replacement,item['id']))
            db.commit()
        if catalog_database is not None:
            _retire_catalog_retries([(get(item['id']),get(replacement))],catalog_database)
        with get_jobs_db() as ready_db:
            operations.transition(ready_db,'youtube',replacement,phase='queued')
        _pending.append(replacement)
        logger.info('Retry queued: job=%s previous=%s phase=%s',replacement,item['id'],'integration' if output else 'download',
                    extra={'event_code':'operation.retry_requested','operation_owner':'youtube','operation_id':replacement,
                           'event_details':{'reason':'user_requested','previous_operation_id':item['id'],
                                            'phase':'integration' if output else 'download'}})
        return get(replacement)


def cancel(job_id):
    with _lock:
        item=get(job_id)
        if not item:raise ValueError('Download not found.')
        if item['status'] in ('queued','downloading','merging'):
            update(job_id,status='cancelled')
            logger.info('Download cancelled: job=%s',job_id)
    return get(job_id)


def recover():
    recovered=[]
    with _lock, get_jobs_db() as db:
        _pending.clear()
        operations.recover_processes(db,'youtube')
        db.execute('BEGIN IMMEDIATE')
        for row in operations.rows(db,"SELECT * FROM youtube_downloads WHERE status IN ('queued','downloading','merging')"):
            values=dict(status='interrupted',error='Interrupted during registration. Retry to register the saved file.' if row['path'] else 'Interrupted. Retry to download again.')
            operational_state.update(db,row,values)
            db.execute("UPDATE youtube_downloads SET status=?,error=? WHERE id=?",(values['status'],values['error'],row['id']))
            recovered.append((row["id"], diagnostics.snapshot(db,"youtube",row["id"])))
        db.commit()

    for identity,state in recovered:
        diagnostics.committed("youtube", identity, state, reason="unfinished_work_recovered")


def child_directory(root,parts):
    current=root
    for part in parts:
        current=current/part
        if current.is_symlink():raise ValueError('Destination contains a symbolic link.')
        current.mkdir(exist_ok=True)
        if current.resolve()!=current:raise ValueError('Destination changed.')
    return current


@observed('youtube', 'publish', identity='job.id', library='job.source_id')
def publish(job, staging, result, *, catalog_database=None):
    with get_jobs_db() as fence:
        operations.validate(fence,'youtube',job['id'])
    catalog_database=catalog_database or operational_state.catalog(get_user_db)
    with exclusive_tool_operation('finishing a YouTube download',wait=False), _lock:
        current=get(job['id'])
        if not current or current['status'] not in ('downloading','merging'):
            raise ValueError('Download was cancelled or its record changed.')
        update(job['id'],failure_phase='publication')
        source,root=destination(job['source_id'],job['root'])
        expected=root/'.keivotos'/'youtube'/job['id']
        if staging!=expected or staging.resolve()!=expected:raise ValueError('Staging location changed.')
        name=result['path'];thumbnail=result.get('thumbnail','')
        for item in [name]+([thumbnail] if thumbnail else []):
            if Path(item).name!=item or '/' in item or '\\' in item or not (staging/item).is_file() or (staging/item).is_symlink():
                raise ValueError('Invalid completed output')
        facts=metadata.validate(result['capture'],job['url'],name) if 'capture' in result else None
        if facts is not None:
            with get_user_db() as db:
                db.execute('BEGIN IMMEDIATE')
                bundle=layout.assign(db,job,root,facts,result['title'])
                db.commit()
            final=layout.resolve(root,bundle)
        else:
            final=child_directory(root,['YouTube'])/job['id']
        if final.exists():raise ValueError('Output already exists; refusing to replace it.')
        with get_user_db() as db:other_roots=[Path(s.path).resolve() for s in sources.list_sources(db) if s.source_id!=source.source_id]
        if any(other.is_relative_to(root) and final.is_relative_to(other) for other in other_roots):
            raise ValueError('Choose the nested destination source directly.')
        if facts is not None:
            local_facts=dict(facts)
            if facts['avatar']:
                asset=staging/facts['avatar']
                if asset.parent.is_symlink() or asset.is_symlink() or not asset.is_file():
                    raise ValueError('Invalid completed profile image')
                with asset.open('rb') as handle:content=handle.read(channel_art.MAX_BYTES+1)
                if asset.suffix!='.'+channel_art.image_type(content):raise ValueError('Profile image format changed')
                facts['avatar']=(final/facts['avatar']).relative_to(root).as_posix()
            from services.item_manifest import collection_identity
            with get_user_db() as collection_db:
                library_id=collection_identity(collection_db,source)
            name,thumbnail,manifest_hash=saved_item.prepare(staging,{**job,'portable_library_id':library_id},result,local_facts)
            if current['metadata_version']:
                if any(current[key]!=value for key,value in facts.items()):raise ValueError('Captured metadata cannot be replaced')
            else:
                # Save facts before the rename: recovery of an already-published file must not fetch them again.
                update(job['id'],**facts,layout_version=1,bundle_path=bundle,manifest_sha256=manifest_hash)
            child_directory(root,list(final.parent.relative_to(root).parts))
            layout.resolve(root,bundle)
            if final.exists():raise ValueError('Output already exists; refusing to replace it.')
        staging.rename(final)
        relative=(final/name).relative_to(root).as_posix()
        from modules.youtube import downloads
        with get_jobs_db() as history_db:
            downloads.complete(history_db,job['id'],result['title'],path=relative,thumbnail=(final/thumbnail).relative_to(root).as_posix() if thumbnail else '',source_role=source.role)
        # Persist publication before optional Files indexing/origin integration.
        update(job['id'],failure_phase='integration',progress=95,title=result['title'],path=relative,
               thumbnail=(final/thumbnail).relative_to(root).as_posix() if thumbnail else '')
        register(get(job['id']),source,root,other_roots,catalog_database=catalog_database)


@observed('youtube', 'register', identity='job.id', library='job.source_id')
def register(job,source,root,other_roots,*,catalog_database=None):
    with get_jobs_db() as fence:
        operations.validate(fence,'youtube',job['id'])
    catalog_database=catalog_database or operational_state.catalog(get_user_db)
    with index.open_index(config.FILES_DB_PATH) as db:
        index.scan_source(db,source.source_id,root,excluded_roots=[config.SUITE_HOME,*other_roots])
        digest=hashing.ensure_index_hash(db,root/job['path'])
    if not digest:raise ValueError('Could not identify completed media.')
    with get_user_db() as db:
        annotations.ensure_annotations_schema(db)
        note=annotations.upsert_annotation(db,subject_kind='file',source_id=source.source_id,relative_path=job['path'],content_hash=digest)
        links=annotations.list_links(db,note.id)
        if not any(link.url==job['url'] for link in links):
            db.execute('INSERT INTO files_annotation_links(annotation_id,url,label,kind,position) VALUES(?,?,?,?,?)',(note.id,job['url'],'YouTube','source',len(links)));db.commit()
    if catalog_database is not None:
        # Explicit staged integration only. The catalog commit precedes the job
        # acknowledgement; these separate databases are not one transaction.
        from modules.youtube.user_schema import _capture_ancestry, register_catalog_publication
        current=get(job['id'])
        if (not current or current['status'] not in ('downloading','merging','complete')
                or any(current[key]!=job[key] for key in ('source_id','root','path'))):
            raise ValueError('Published registration record changed.')
        source,root=destination(current['source_id'],current['root'])
        with get_jobs_db() as db:
            owners=_capture_ancestry(db,current)
            for child,parent in zip(owners,owners[1:]):
                link=db.execute('SELECT superseded_by FROM youtube_downloads WHERE id=?',(parent,)).fetchone()
                if not link or link['superseded_by']!=child:
                    raise ValueError('The accepted retry history changed.')
        with get_user_db() as db:
            forbidden=[config.SUITE_HOME,*[Path(child.path) for child in
                sources.descendant_sources(source,sources.list_sources(db))]]
        with get_jobs_db() as receipt_db:
            operation=operations.get(receipt_db,'youtube',job['id'])
            from services.job_store import read_request
            receipt=read_request(receipt_db,owner='youtube',request_id=operation['request_id'])
        with catalog_database() as db:
            if db.in_transaction:raise ValueError('Open a clean catalog registration connection.')
            db.execute('BEGIN IMMEDIATE')
            try:
                register_catalog_publication(db,current,legacy_job_ids=owners,source_id=source.source_id,
                    source_root=root,forbidden_roots=forbidden)
                if current.get('layout_version'):
                    from services import item_manifest as portable
                    relative='/'.join(p for p in (current['bundle_path'],portable.MANIFEST) if p)
                    from storage_layout import portable_path
                    with portable_path(root,relative,forbidden).open('rb') as stream:
                        raw=stream.read(portable.MAX_BYTES+1)
                    parsed=json.loads(raw,object_pairs_hook=portable._unique)
                    if isinstance(parsed,dict) and parsed.get('format')==portable.FORMAT:
                        value,observations=portable.read(root,relative,forbidden=forbidden,hash_assets=True)
                        with get_user_db() as source_db:registered=sources.list_sources(source_db)
                        context=portable.Context(source,tuple(registered),config.SUITE_HOME,config.FILES_DB_PATH,db,config.MODULE_REGISTRY)
                        portable.register(value,context,relative,observations=observations,register_owner=False)
                operational_state.register_ack(db,receipt,current['id'])
                # Hashing can take time. Recheck current authorization and the
                # publication once more before committing the essential save.
                source,root=destination(current['source_id'],current['root'])
                with get_user_db() as source_db:
                    forbidden=[config.SUITE_HOME,*[Path(child.path) for child in
                        sources.descendant_sources(source,sources.list_sources(source_db))]]
                if metadata.preserved_media(db,current['id'],source_id=source.source_id,
                        source_root=root,forbidden_roots=forbidden,hash_assets=False) is None:
                    raise ValueError('Published registration became unavailable.')
                db.commit()
            except BaseException:
                db.rollback()
                raise
        with get_jobs_db() as state_db:
            current_state=operations.get(state_db,'youtube',job['id'])
            if current_state['state'] not in operations.TERMINAL:
                operations.transition(state_db,'youtube',job['id'],publication='catalog-registered')
    update(job['id'],status='complete',progress=100,error='',failure_phase='')
    logger.info('Download complete: job=%s file=%s',job['id'],root/job['path'])


@observed('youtube', 'registration.repair', identity='job.id', library='job.source_id')
def resume_registration(job, *, catalog_database=None):
    with get_jobs_db() as fence:
        operations.validate(fence,'youtube',job['id'])
    catalog_database=catalog_database or operational_state.catalog(get_user_db)
    with exclusive_tool_operation('registering a saved YouTube download',wait=False), _lock:
        current=get(job['id'])
        if not current or current['status'] not in ('downloading','merging'):
            raise ValueError('Download was cancelled or its record changed.')
        source,root=destination(current['source_id'],current['root'])
        output=published_output(current,root)
        if not output:raise ValueError('Published output is unavailable. Files were preserved.')
        with get_user_db() as db:other_roots=[Path(s.path).resolve() for s in sources.list_sources(db) if s.source_id!=source.source_id]
        update(job['id'],**output,failure_phase='integration')
        register(get(job['id']),source,root,other_roots,catalog_database=catalog_database)


async def terminate(process):
    if process.returncode is not None:return
    if sys.platform=='win32':
        killer=await asyncio.create_subprocess_exec('taskkill','/PID',str(process.pid),'/T','/F',stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,**quiet_options())
        await killer.wait()
    else:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
    await process.wait()


@observed('youtube', 'download', identity='job.id', library='job.source_id', false_outcome='cancelled')
async def execute(job):
    source,root=destination(job['source_id'],job['root'])
    parent=child_directory(root,['.keivotos','youtube'])
    staging=parent/job['id'];staging.mkdir(exist_ok=False)
    tools=dependencies()
    if not tools['ready']:raise ValueError('Missing: '+', '.join(tools['missing']))
    command=([sys.executable,'--youtube-worker'] if getattr(sys,'frozen',False)
             else [sys.executable,str(config.CODE_ROOT/'app.py'),'--youtube-worker'])
    launch=asyncio.create_task(asyncio.create_subprocess_exec(*command, env=diagnostics.worker_environment(os.environ, profile=config.SUITE_HOME),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
        **({'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {'start_new_session':True})))
    try:process=await asyncio.shield(launch)
    except asyncio.CancelledError:
        process=await launch;await terminate(process);raise
    evidence=ToolEvidence("youtube", command, destination=staging, packages=("yt-dlp",))
    diagnostics.emit("youtube", "tool.dependency", "Configured FFmpeg", details=command_details([tools["ffmpeg"]]))
    result=None
    async def events():
        nonlocal result
        while line:=await process.stdout.readline():
            evidence.output(line, "stdout", protocol=True)
            event=json.loads(line)
            if event.get('event')=='diagnostic' and event.get('code')=='ffmpeg':
                facts=event.get('facts') or {}
                diagnostics.emit('youtube','tool.ffmpeg_invoked','FFmpeg invoked',
                    details={key:facts[key] for key in ('executable','tool_version','arguments') if key in facts})
            elif event.get('event')=='complete':result=event
            elif event.get('event')=='metadata':
                current=get(job['id'])
                title=event.get('title')
                if current and current['status'] not in ('cancelled','interrupted') and isinstance(title,str) and title.strip():
                    update(job['id'],title=title[:512])
            elif event.get('event')=='file':
                from modules.youtube import downloads
                current=get(job['id'])
                if not current or current['status'] in ('cancelled','interrupted'):continue
                try:
                    with get_jobs_db() as history_db:
                        downloads.event(history_db,job['id'],event,source_role=source.role)
                except operations.StaleAttempt:
                    current=get(job['id'])
                    if not current or current['status'] in ('cancelled','interrupted'):return
                    raise
            elif get(job['id']) and get(job['id'])['status'] not in ('cancelled','interrupted'):
                if event.get('event')=='progress':
                    update(job['id'],progress=max(get(job['id'])['progress'],min(95,float(event['progress']))),title=str(event['title'])[:512])
                elif event.get('event')=='merging':update(job['id'],status='merging')
    async def read_diagnostics():
        while line:=await process.stderr.readline():
            evidence.output(line, "stderr")
            logger.info("job=%s %s",job["id"],line.decode("utf-8","replace").strip()[:2000])
    validated=False
    tasks=[]
    try:
        with get_jobs_db() as process_db:
            operations.register_process(process_db,'youtube',job['id'],process.pid)
            from services import download_controls
            download_controls.register_worker(process_db,'youtube',job['id'],process.pid)
        process.stdin.write(json.dumps({**job,'directory':str(staging),'ffmpeg':tools['ffmpeg']}).encode()+b'\n')
        await process.stdin.drain();process.stdin.close()
        tasks=[asyncio.create_task(events()),asyncio.create_task(read_diagnostics())]
        while process.returncode is None:
            item=get(job['id'])
            if not item or item['status']=='cancelled':await terminate(process);return False
            if any(t.done() and t.exception() for t in tasks):raise RuntimeError('Downloader event stream failed')
            await asyncio.sleep(.2)
        await asyncio.gather(*tasks)
        current=get(job['id'])
        if not current or current['status']=='cancelled':return False
        if process.returncode or not result:raise RuntimeError('Download failed. See Logs.')
        validated=True
        completion=asyncio.create_task(asyncio.to_thread(publish,job,staging,result))
        try:await asyncio.shield(completion)
        except asyncio.CancelledError:await completion;raise
    finally:
        escaped=sys.exc_info()[1]
        await terminate(process)
        from services import download_controls
        with get_jobs_db() as process_db:download_controls.unregister_worker(process_db,'youtube',job['id'],process.pid)
        try:current=get(job["id"])
        except Exception:current=None
        state=current["status"] if current else "interrupted"
        outcome="cancelled" if state=="cancelled" else "interrupted" if state=="interrupted" or isinstance(escaped,asyncio.CancelledError) else "failed" if not validated else None
        evidence.finish(process.returncode, outcome=outcome,
                        reason="user_cancelled" if state=="cancelled" else "shutdown_or_disable" if isinstance(escaped,asyncio.CancelledError) else None, validation=validated)
        for task in tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


async def run_queue():
    try:
        while True:
            with _lock, get_jobs_db() as db:
                db.execute('BEGIN IMMEDIATE')
                from services.download_controls import paused
                row=None if paused(db) else db.execute("SELECT n.* FROM youtube_downloads n JOIN kv_operations o ON o.owner='youtube' AND o.id=n.id WHERE n.status='queued' AND o.state='requested' AND o.phase!='retry-pending' ORDER BY n.rowid LIMIT 1").fetchone()
                if row:
                    active=dict(row)
                    operational_state.ensure(db,active)
                    token=operations.claim(db,'youtube',active['id'])
                    db.execute("UPDATE youtube_downloads SET status='downloading' WHERE id=?",(active['id'],))
                    if active['id'] in _pending:_pending.remove(active['id'])
                db.commit()
            if not row:
                await asyncio.sleep(.2)
                continue
            job_id=active['id']
            with operations.bind('youtube',job_id,token):
                diagnostics.emit("youtube", "operation.attempt_started", "Download attempt started",
                                 details={"attempt_kind":"download"})
                try:
                    if active['path']:
                        completion=asyncio.create_task(asyncio.to_thread(resume_registration,active))
                        try:await asyncio.shield(completion)
                        except asyncio.CancelledError:await completion;raise
                    else:await execute(active)
                except asyncio.CancelledError:raise
                except operations.StaleAttempt:
                    logger.info('Obsolete YouTube attempt ignored: job=%s',job_id)
                except Exception:
                    current=get(job_id)
                    phase=(current['failure_phase'] or 'download') if current else 'download'
                    logger.exception('YouTube %s failed: job=%s; files retained',phase,job_id)
                    message={'integration':'Video saved, but registration failed. Retry to register the saved file. See Logs.',
                             'publication':'Download finished, but saving failed. Files were preserved. See Logs.'}.get(phase,'Download failed. See Logs.')
                    if current and current['status'] not in ('cancelled','interrupted','complete'):
                        update(job_id,status='failed',error=message,failure_phase=phase)
    finally:
        recover()
        logger.info('YouTube worker stopped; unfinished downloads marked interrupted')
