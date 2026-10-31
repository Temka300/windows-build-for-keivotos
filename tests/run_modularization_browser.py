"""Build current UI into scratch space and run timed modularization checks.

Requires external Node, Playwright and Chromium; does not install dependencies,
change frontend/dist, restart the live server or use the real library.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
SERVER = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'backend'))
from runtime_logging import configure_runtime_logging
configure_runtime_logging()
import server
from fastapi.staticfiles import StaticFiles
import uvicorn
server.FRONTEND_DIST = Path(sys.argv[1])
for route in server.app.routes:
    if getattr(route, 'name', None) == 'frontend':
        route.app = StaticFiles(directory=sys.argv[1], html=True)
if len(sys.argv)>3 and sys.argv[3] in ('youtube','video'):
    sys.path.insert(0, str(Path.cwd() / 'tests'))
    from browser_youtube_fixture import install
    install(Path(sys.argv[4]))
if len(sys.argv)>3 and sys.argv[3]=='twitter':
    sys.path.insert(0, str(Path.cwd() / 'tests'))
    from browser_twitter_fixture import install
    install(Path(sys.argv[4]))
if len(sys.argv)>3 and sys.argv[3]=='diagnostics':
    sys.path.insert(0,str(Path.cwd()/'tests'))
    from browser_diagnostics_fixture import install
    install()
if len(sys.argv)>3 and sys.argv[3]=='home_empty':
    sys.path.insert(0,str(Path.cwd()/'tests'))
    from browser_home_empty_fixture import install
    install(Path(sys.argv[4]))
if len(sys.argv)>3 and sys.argv[3]=='downloads':
    sys.path.insert(0,str(Path.cwd()/'tests'))
    from browser_downloads_fixture import install
    install(Path(sys.argv[4]))
uvicorn.run(server.app, host='127.0.0.1', port=int(sys.argv[2]), log_config=None)
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', default=shutil.which('node'))
    parser.add_argument('--port', type=int, default=0, help='Optional available loopback port for host browser forwarding')
    parser.add_argument('--dist', type=Path, help='Prebuilt scratch frontend; skip the runner build')
    parser.add_argument('--script', choices=('modularization', 'backup', 'danbooru', 'settings', 'import', 'modules', 'settings_failures', 'appearance', 'video', 'manga', 'youtube', 'language', 'ost', 'twitter', 'module_locations', 'file_paths', 'storage', 'absence', 'diagnostics', 'module_search', 'downloads', 'home_empty'), default='modularization')
    parser.add_argument('--output', required=True, type=Path, help='New directory for logs, screenshots and timing report')
    args = parser.parse_args()
    if not args.node:
        parser.error('Provide --node /path/to/node')
    windows_node = str(args.node).lower().endswith('.exe')

    def node_path(path: Path) -> str:
        if not windows_node:
            return str(path)
        return subprocess.check_output(['wslpath', '-w', str(path)], text=True).strip()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='keivotos-modularization-') as temporary:
        base = Path(temporary)
        home = base / 'home'
        home.mkdir()
        os.environ['KEIVOTOS_HOME'] = str(home)
        (home / 'config.json').write_text(json.dumps({'default_library_created': True}))
        if args.script == 'settings':
            (home / 'config.json').write_text(json.dumps({'default_library_created': True, 'show_experimental_modules': True}))
            media = base / 'media'
            (media / 'Copied library 한글').mkdir(parents=True)
            (media / 'Copied library 한글' / 'one.txt').write_text('preserved copied file', encoding='utf-8')
            for name in ('Copied library 한글', 'Reverse library'):
                nested = media / name / 'hehetwitter'
                nested.mkdir(parents=True)
                (nested / 'post.txt').write_text('preserved nested file', encoding='utf-8')
            sys.path.insert(0, str(ROOT / 'backend'))
            from files_base import sources, annotations
            with sqlite3.connect(home / 'user.sqlite') as connection:
                sources.ensure_sources_schema(connection)
                annotations.ensure_annotations_schema(connection)
                connection.execute("INSERT INTO files_sources(source_id,path,display_name,role,visible) VALUES('copied-windows-root',?,'Copied library','files',1)", ('D:\\Library',))
                connection.execute("INSERT INTO files_annotations(subject_kind,source_id,relative_path,description) VALUES('dir','copied-windows-root','','Preserved folder note')")
                for source_id, path, name, role in (
                    ('copied-child-root', 'D:\\Library\\hehetwitter', 'Nested child', 'twitter'),
                    ('reverse-parent', 'E:\\Library', 'Reverse library', 'files'),
                    ('reverse-child', 'E:\\Library\\hehetwitter', 'Reverse child', 'twitter')):
                    connection.execute('INSERT INTO files_sources(source_id,path,display_name,role,visible) VALUES(?,?,?,?,1)', (source_id, path, name, role))
                    connection.execute("INSERT INTO files_annotations(subject_kind,source_id,relative_path,description) VALUES('dir',?,'','Preserved child note')", (source_id,))
            from files_base.schema import ensure_files_schema
            (home / 'base').mkdir(exist_ok=True)
            with sqlite3.connect(home / 'base/files.sqlite') as connection:
                ensure_files_schema(connection)
                for source_id, path in (('copied-child-root', 'D:\\Library\\hehetwitter\\post.txt'), ('reverse-child', 'E:\\Library\\hehetwitter\\post.txt')):
                    connection.execute("INSERT INTO files_index(source_id,path,relative_path,parent,name,is_dir,available) VALUES(?,?,'post.txt','','post.txt',0,1)", (source_id, path))
        if args.script == 'storage':
            thumbnails=home/'base/thumbnails';thumbnails.mkdir(parents=True)
            for suffix in ('','_600','_1200'):
                (thumbnails/('a'*32+'_v4'+suffix+'.webp')).write_bytes(b'x'*1048576)
            (thumbnails/('b'*32+'_v3.webp')).write_bytes(b'x'*1048576)
            (thumbnails/'keep.json').write_text('preserved')
            media=base/'media';media.mkdir()
            (media/'indexed.bin').write_bytes(b'x'*1536)
        if args.script in ('danbooru', 'home_empty'):
            from browser_library import seed_library
            seed_library(home)
        if args.script == 'video':
            from browser_video_fixture import seed_videos
            seed_videos(base / 'media')
        if args.script == 'manga':
            from browser_manga_fixture import seed_manga
            seed_manga(base / 'media')
        if args.script == 'youtube':
            from browser_youtube_fixture import seed
            seed(base / 'media')
        if args.script == 'downloads':
            from browser_downloads_fixture import seed
            seed(base/'media')
        if args.script == 'language':
            from browser_language_fixture import seed_language
            seed_language(base / 'media')
        if args.script == 'ost':
            from browser_ost_fixture import seed_ost, seed_files_metadata
            seed_ost(base / 'media')
            sys.path.insert(0, str(ROOT / 'backend'))
            seed_files_metadata(base / 'metadata-samples', home)
        if args.script == 'twitter':
            from browser_twitter_fixture import seed_twitter
            seed_twitter(base / 'media')
        if args.script == 'module_locations':
            media = base / 'media'
            (media / 'Deep' / '子').mkdir(parents=True)
            (media / 'Nested').mkdir()
            (media / 'Deep' / '子' / 'same.txt').write_text('outer', encoding='utf-8')
            (media / 'Nested' / 'same.txt').write_text('inner', encoding='utf-8')
        if args.script == 'file_paths':
            media = base / 'media'
            for name in ('CaseLibrary', 'caselibrary'):
                (media / name / 'Deep' / '子').mkdir(parents=True)
                (media / name / 'ExactName.txt').write_text(name, encoding='utf-8')
                (media / name / 'Deep' / '子' / 'Same.txt').write_text(name + ' nested', encoding='utf-8')
        if args.script == 'module_search':
            from browser_library import seed_library
            from browser_twitter_fixture import write_capture
            from PIL import Image
            from io import BytesIO
            import wave
            seed_library(home)
            settings = json.loads((home / 'config.json').read_text())
            settings['show_experimental_modules'] = True
            (home / 'config.json').write_text(json.dumps(settings))
            media = base / 'media'
            for owner in ('Files', 'Video', 'Manga', 'Twitter', 'Music'):
                (media / owner).mkdir(parents=True)
            for number in range(215):
                (media / 'Files' / f'match-{number:03d}.txt').write_text('preserved search fixture')
            for name in ('ÉCOLE.mp4', 'Straße.mp4', '한국어 100%_.mp4'):
                (media / 'Video' / name).write_bytes(b'local search fixture, never played')
            for name in ('ÉCOLE', 'Straße', '한국어 100%_'):
                chapter = media / 'Manga' / name
                chapter.mkdir()
                Image.new('RGB', (32, 48), '#59427a').save(chapter / 'page.png')
            avatar = BytesIO()
            Image.new('RGB', (16, 16), '#59427a').save(avatar, 'JPEG')
            for number, author in enumerate(('123', '456')):
                write_capture(media / 'Twitter', number, text='hello 한국어 100%_', author_id=author,
                              handle='Saved' + author, avatar=avatar.getvalue(), photos={}, current_format=True)
            with wave.open(str(media / 'Music' / 'music 한글.wav'), 'wb') as output_audio:
                output_audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                output_audio.writeframes(b'\0\0' * 800)
        project = ROOT
        if args.script == 'absence':
            project = base / 'source'
            project.mkdir()
            shutil.copytree(ROOT / 'backend', project / 'backend',
                            ignore=shutil.ignore_patterns('danbooru', 'video', 'manga', 'youtube', 'language', 'ost', 'twitter', '__pycache__'))
            for name in ('app.py', 'config.json'):
                shutil.copy2(ROOT / name, project / name)
            shutil.copytree(ROOT / 'frontend', project / 'frontend',
                            ignore=shutil.ignore_patterns('node_modules', 'dist', 'danbooru', 'video', 'manga', 'youtube', 'language', 'ost', 'twitter'))
            (project / 'frontend/node_modules').symlink_to(ROOT / 'frontend/node_modules', target_is_directory=True)
            media = base / 'media'
            media.mkdir()
            (media / 'preserved.txt').write_text('Files-only preservation fixture')
        dist = args.dist.resolve() if args.dist else (
            project / 'frontend/dist' if args.script == 'absence' else base / 'frontend')
        if args.script in ('module_locations', 'file_paths'):
            # Expose only the internal navigation action in a disposable build.
            # The actual Files/registry code is copied unchanged and exercised
            # from the same origin as the isolated backend.
            scratch_frontend = base / 'frontend-source'
            shutil.copytree(ROOT / 'frontend', scratch_frontend,
                            ignore=shutil.ignore_patterns('node_modules', 'dist', '__pycache__'))
            (scratch_frontend / 'node_modules').symlink_to(ROOT / 'frontend/node_modules', target_is_directory=True)
            app_source = scratch_frontend / 'src/App.svelte'
            app_text = app_source.read_text()
            app_text = app_text.replace("  import { onMount } from 'svelte';",
                                        "  import { onMount } from 'svelte';\n  import { showInFiles } from './modules/registry';")
            app_text = app_text.replace('  onMount(() => { void initializeModules(); });',
                                        '  onMount(() => { (window as any).__showInFiles = showInFiles; void initializeModules(); });')
            app_source.write_text(app_text)
            dist = base / 'frontend'
            build_node = shutil.which('node') or args.node
            with (output / 'build.log').open('w') as build_log:
                subprocess.run([build_node, 'node_modules/vite/bin/vite.js', 'build', '--outDir', str(dist)],
                               cwd=scratch_frontend, stdout=build_log, stderr=subprocess.STDOUT, check=True)
        if args.dist and args.script == 'absence':
            placeholder = project / 'frontend/dist/index.html'
            placeholder.parent.mkdir(parents=True, exist_ok=True)
            placeholder.write_text('scratch frontend route placeholder')
        if not args.dist and args.script not in ('module_locations', 'file_paths'):
            with (output / 'build.log').open('w') as log:
                build_node = (shutil.which('node') or args.node) if windows_node else args.node
                subprocess.run([build_node, 'node_modules/vite/bin/vite.js', 'build', '--outDir', str(dist)],
                               cwd=project / 'frontend', stdout=log, stderr=subprocess.STDOUT, check=True)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', args.port))
            port = probe.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        context = base / 'context.json'
        context.write_text(json.dumps({'url': url, 'home': str(home), 'output': node_path(output),
                                       'media': str(base / 'media'), 'mediaLocal': node_path(base / 'media')}))
        env = {**os.environ, 'KEIVOTOS_HOME': str(home)}
        with (output / 'server.log').open('w') as log:
            server = subprocess.Popen([sys.executable, '-c', SERVER, str(dist), str(port), args.script, str(base / 'media')],
                                      cwd=project, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if server.poll() is not None:
                        raise RuntimeError('Isolated server exited; inspect server.log')
                    try:
                        with urllib.request.urlopen(url, timeout=.5):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('Isolated server startup timed out')
                result = subprocess.run([args.node, node_path(ROOT / 'tests' / f'browser_{args.script}.cjs'),
                                         node_path(context)],
                                        cwd=ROOT, env=env, timeout=300 if args.script in ('youtube','language','ost','downloads','home_empty') else 180)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
        diagnostic_files=list((home/'logs').glob('*/events-0001.jsonl*'))
        if diagnostic_files:
            event_output=output/'diagnostic-events'
            event_output.mkdir(exist_ok=True)
            for number,file in enumerate(diagnostic_files):
                shutil.copyfile(file,event_output/(str(number)+'-'+file.name))
        if args.script == 'diagnostics' and result.returncode == 0:
            record=json.loads((output/'report.json').read_text())
            raw='\n'.join(file.read_text() for file in diagnostic_files)
            assert 'private diagnostic fixture body' not in raw
            assert 'private script fixture body' not in raw
            assert 'private rejection fixture body' not in raw
            assert 'private invalid body' not in raw
            events=[json.loads(line) for file in diagnostic_files for line in file.read_text().splitlines()]
            matching=[event for event in events if event.get('operation_id')==record['failedOperation']]
            assert {'http.request_rejected','frontend.request_failed'} <= {event['event_code'] for event in matching}
            assert record['failedOperation'] in (output/'server.log').read_text()
            (output/'correlation.json').write_text(json.dumps({'operation_id':record['failedOperation'],
                'backend_frontend_terminal_file_match':True,'private_contents_excluded':True},indent=2))
        if args.script == 'absence':
            assert not (home / 'modules/danbooru').exists(), 'Absent module created storage'
            with sqlite3.connect(home / 'keivotos.sqlite') as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'user_settings' in tables
            assert not {'favorites', 'collections', 'collection_items'} & tables
            assert (base / 'media/preserved.txt').read_text() == 'Files-only preservation fixture'
        if args.script == 'settings' and result.returncode == 0:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0)); restart_port = probe.getsockname()[1]
            restart_url = f'http://127.0.0.1:{restart_port}'
            with (output / 'restart.log').open('w') as log:
                restarted = subprocess.Popen([sys.executable, '-c', SERVER, str(dist), str(restart_port)],
                    cwd=project, env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        try:
                            with urllib.request.urlopen(restart_url + '/api/files/sources', timeout=.5) as response:
                                rows = json.load(response)
                            break
                        except OSError:
                            if restarted.poll() is not None: raise RuntimeError('Restarted fixture server exited')
                            time.sleep(.1)
                    else: raise RuntimeError('Restarted fixture server timed out')
                    copied = [row for row in rows if row['source_id'] == 'copied-windows-root']
                    assert len(copied) == 1 and copied[0]['path'] == str(base / 'media/Copied library 한글')
                    with urllib.request.urlopen(restart_url + '/api/files/browse?source_id=copied-windows-root') as response:
                        assert any(row['name'] == 'one.txt' for row in json.load(response))
                    with sqlite3.connect(home / 'keivotos.sqlite') as connection:
                        assert connection.execute("SELECT description FROM files_annotations WHERE source_id='copied-windows-root'").fetchone()[0] == 'Preserved folder note'
                    assert (base / 'media/Copied library 한글/one.txt').read_text() == 'preserved copied file'
                    for source_id, relative in (('copied-child-root', 'Copied library 한글/hehetwitter'), ('reverse-parent', 'Reverse library'), ('reverse-child', 'Reverse library/hehetwitter')):
                        assert next(row for row in rows if row['source_id'] == source_id)['path'] == str(base / 'media' / relative)
                    for source_id in ('copied-child-root', 'reverse-child'):
                        with urllib.request.urlopen(restart_url + '/api/files/browse?source_id=' + source_id) as response:
                            assert any(row['name'] == 'post.txt' for row in json.load(response))
                    for relative in ('Copied library 한글/hehetwitter/post.txt', 'Reverse library/hehetwitter/post.txt'):
                        assert (base / 'media' / relative).read_text() == 'preserved nested file'
                    category_names = {}
                    for owner in ('video', 'manga'):
                        with urllib.request.urlopen(restart_url + '/api/' + owner + '/categories') as response:
                            category_names[owner] = [item['name'] for item in json.load(response)['items']]
                    assert category_names == {'video': ['공부 音楽'], 'manga': ['공부 音楽', 'Retry shelf', 'IME shelf']}
                    (output / 'restart.json').write_text(json.dumps({'passed': True, 'source_id': 'copied-windows-root', 'original_bytes_preserved': True, 'categories': category_names}))
                finally:
                    restarted.terminate()
                    try: restarted.wait(timeout=20)
                    except subprocess.TimeoutExpired: restarted.kill(); restarted.wait()
            if 'ERROR:' in (output / 'restart.log').read_text(): raise RuntimeError('Restart logged an error')
        if args.script in ('video', 'manga') and result.returncode == 0:
            expected = json.loads((output / 'report.json').read_text())['categories']
            owner = expected['owner']
            with sqlite3.connect(home / 'keivotos.sqlite') as connection:
                tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?", (owner + '_categor%',))]
                before = {name: connection.execute(f'SELECT * FROM {name} ORDER BY rowid').fetchall() for name in tables}
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0)); restart_port = probe.getsockname()[1]
            restart_url = f'http://127.0.0.1:{restart_port}'
            with (output / 'restart.log').open('w') as log:
                restarted = subprocess.Popen([sys.executable, '-c', SERVER, str(dist), str(restart_port)], cwd=project, env=env, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        try:
                            with urllib.request.urlopen(restart_url + '/api/' + owner + '/library?' + urlencode({'category': expected['category_id']}), timeout=.5) as response:
                                library = json.load(response)
                            break
                        except OSError:
                            if restarted.poll() is not None: raise RuntimeError('Restarted category server exited')
                            time.sleep(.1)
                    else: raise RuntimeError('Restarted category server timed out')
                    assert library['total'] == expected['total']
                    with urllib.request.urlopen(restart_url + '/api/' + owner + '/category-membership?' + urlencode(expected['item'])) as response:
                        membership = json.load(response)
                    assert membership['category_ids'] == expected['category_ids']
                    with sqlite3.connect(home / 'keivotos.sqlite') as connection:
                        assert before == {name: connection.execute(f'SELECT * FROM {name} ORDER BY rowid').fetchall() for name in tables}
                    (output / 'restart.json').write_text(json.dumps({'passed': True, 'owner': owner, 'total': library['total'], 'membership': membership, 'all_category_rows_preserved': True}))
                    if args.script in ('video','manga') and 'continuation' in json.loads((output/'report.json').read_text()):
                        continuation = json.loads((output / 'report.json').read_text())['continuation']
                        restarted_output = output / 'restart-browser'
                        restarted_output.mkdir()
                        restarted_context = base / 'restart-context.json'
                        restarted_context.write_text(json.dumps({'url': restart_url, 'output': node_path(restarted_output), 'continuation': continuation}))
                        resumed = subprocess.run([args.node, node_path(ROOT / ('tests/browser_'+args.script+'.cjs')), node_path(restarted_context)],
                                                 cwd=ROOT, env=env, timeout=45)
                        if resumed.returncode: raise RuntimeError(args.script+' continuation failed to resume after server restart')
                finally:
                    restarted.terminate()
                    try: restarted.wait(timeout=20)
                    except subprocess.TimeoutExpired: restarted.kill(); restarted.wait()
            if 'ERROR:' in (output / 'restart.log').read_text(): raise RuntimeError('Restart logged an error')
        if args.script == 'youtube' and result.returncode == 0:
            expected=json.loads((output/'report.json').read_text())['metadata']
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0));restart_port=probe.getsockname()[1]
            restart_url=f'http://127.0.0.1:{restart_port}'
            with (output/'restart.log').open('w') as log:
                restarted=subprocess.Popen([sys.executable,'-c',SERVER,str(dist),str(restart_port)],cwd=project,env=env,stdout=log,stderr=log)
                try:
                    deadline=time.monotonic()+30
                    while time.monotonic()<deadline:
                        try:
                            with urllib.request.urlopen(restart_url+'/api/youtube/library?'+urlencode({'q':expected['title']}),timeout=.5) as response:
                                saved=next(item for item in json.load(response)['items'] if item['id']==expected['id'])
                            break
                        except OSError:
                            if restarted.poll() is not None:raise RuntimeError('Restarted YouTube server exited')
                            time.sleep(.1)
                    else:raise RuntimeError('Restarted YouTube server timed out')
                    assert {name:saved[name] for name in expected['fields']}==expected['fields']
                    channel_key=expected['fields'].get('channel_id') or 'unknown'
                    with urllib.request.urlopen(restart_url+'/api/youtube/channels')as response:
                        assert any(row['id']==channel_key for row in json.load(response)['items'])
                    with urllib.request.urlopen(restart_url+'/api/youtube/channels/'+channel_key+'/videos?'+urlencode({'q':expected['title']}))as response:
                        assert any(row['id']==expected['id']for row in json.load(response)['items'])
                    if expected.get('details'):
                        with urllib.request.urlopen(restart_url+'/api/youtube/videos/'+expected['id']+'/details')as response:
                            assert json.load(response)==expected['details']
                        with urllib.request.urlopen(restart_url+'/api/youtube/videos/'+expected['id']+'/description')as response:
                            assert response.read()==expected['details']['description'].encode('utf-8')
                    with urllib.request.urlopen(restart_url+'/api/youtube/channel-art/'+expected['id']) as response:
                        assert response.headers.get_content_type()=='image/png'
                        assert hashlib.sha256(response.read()).hexdigest()==expected['avatar_sha256']
                    restarted_output=output/'restart-browser';restarted_output.mkdir()
                    restarted_context=base/'restart-context.json'
                    restarted_context.write_text(json.dumps({'url':restart_url,'output':node_path(restarted_output),'metadata':expected}))
                    resumed=subprocess.run([args.node,node_path(ROOT/'tests/browser_youtube.cjs'),node_path(restarted_context)],cwd=ROOT,env=env,timeout=45)
                    if resumed.returncode:raise RuntimeError('YouTube channel cards failed after restart')
                    (output/'restart.json').write_text(json.dumps({'passed':True,'metadata_fields_preserved':True,'avatar_hash_preserved':True,'saved_details_preserved':bool(expected.get('details')),'channel_grouping_preserved':True}))
                finally:
                    restarted.terminate()
                    try:restarted.wait(timeout=20)
                    except subprocess.TimeoutExpired:restarted.kill();restarted.wait()
            if 'ERROR:' in (output/'restart.log').read_text():raise RuntimeError('Restart logged an error')
        if args.script == 'language' and result.returncode == 0:
            expected=json.loads((output/'report.json').read_text())['vocabulary']
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0));restart_port=probe.getsockname()[1]
            restart_url=f'http://127.0.0.1:{restart_port}'
            with (output/'restart.log').open('w') as log:
                restarted=subprocess.Popen([sys.executable,'-c',SERVER,str(dist),str(restart_port)],cwd=project,env=env,stdout=log,stderr=log)
                try:
                    deadline=time.monotonic()+30
                    while time.monotonic()<deadline:
                        try:
                            with urllib.request.urlopen(restart_url+'/api/language/vocabulary/'+expected['archived']['id'],timeout=.5) as response:
                                assert json.load(response)==expected['archived']
                            break
                        except OSError:
                            if restarted.poll() is not None:raise RuntimeError('Restarted Language server exited')
                            time.sleep(.1)
                    else:raise RuntimeError('Restarted Language server timed out')
                    restarted_output=output/'restart-browser';restarted_output.mkdir()
                    restarted_context=base/'restart-context.json'
                    restarted_context.write_text(json.dumps({'url':restart_url,'home':node_path(home),'output':node_path(restarted_output),'vocabulary':expected}))
                    resumed=subprocess.run([args.node,node_path(ROOT/'tests/browser_language.cjs'),node_path(restarted_context)],cwd=ROOT,env=env,timeout=45)
                    if resumed.returncode:raise RuntimeError('Vocabulary edit/delete state failed after restart')
                    (output/'restart.json').write_text(json.dumps({'passed':True,'vocabulary_history_preserved':True}))
                finally:
                    restarted.terminate()
                    try:restarted.wait(timeout=20)
                    except subprocess.TimeoutExpired:restarted.kill();restarted.wait()
            if 'ERROR:' in (output/'restart.log').read_text():raise RuntimeError('Restart logged an error')
        log_text = (output / 'server.log').read_text()
        if 'Traceback (most recent call last)' in log_text or 'ERROR:' in log_text:
            raise RuntimeError('Isolated server logged errors; inspect server.log')
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
