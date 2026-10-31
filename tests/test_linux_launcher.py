"""Real shell/desktop parsing against generated, isolated application folders."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux shell/desktop integration')


@pytest.fixture
def package(tmp_path):
    stage = tmp_path / 'app 한글 with spaces'
    stage.mkdir()
    for name in ('start-keivotos.sh', 'start-portable.sh', 'install-desktop.sh', 'Keivotos.desktop.in'):
        shutil.copyfile(ROOT / 'packaging/linux' / name, stage / name)
    (stage / 'Keivotos').write_text('''#!/bin/sh
case "${1:-}" in
    --portable-check) printf 'Writable home: %s\\n' "${KEIVOTOS_HOME:-$TEST_DATA}"; exit "${TEST_RESOURCE_EXIT:-0}" ;;
    --version) printf 'fixture version\\n'; exit 0 ;;
esac
printf '%s\\0' "$PWD" "${KEIVOTOS_HOME-}" "$@" > "$TEST_RECORD"
exit "${TEST_EXIT:-0}"
''')
    for name in ('Keivotos', 'ffmpeg'):
        if name != 'Keivotos': (stage / name).write_text('#!/bin/sh\nexit 0\n')
        (stage / name).chmod(0o755)
    (stage / 'linux-tools.txt').write_text('ffmpeg\n')
    icon = stage / '_internal/assets/branding/keivotos/keivotos-icon-256.png'
    icon.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / 'assets/branding/keivotos/keivotos-icon-256.png', icon)
    environment = os.environ.copy()
    for key in ('KEIVOTOS_HOME', 'XDG_DATA_HOME'):
        environment.pop(key, None)
    environment.update(TEST_DATA=str(tmp_path / 'normal-data'), TEST_RECORD=str(tmp_path / 'record'),
                       XDG_DATA_HOME=str(tmp_path / 'xdg'), HOME=str(tmp_path / 'home'))
    return stage, environment


def launch(package, script='start-keivotos.sh', *arguments):
    stage, environment = package
    return subprocess.run(['/bin/sh', str(stage / script), *arguments], env=environment,
                          cwd=stage.parent, capture_output=True, text=True, timeout=10)


def record(package):
    return Path(package[1]['TEST_RECORD']).read_bytes().decode().split('\0')[:-1]


def test_normal_launch_forwards_cwd_arguments_and_exit_status(package):
    package[1]['TEST_EXIT'] = '7'
    result = launch(package, 'start-keivotos.sh', '--no-browser', '--port', '53212', '한 글')
    assert result.returncode == 7
    assert record(package) == [str(package[0]), '', '--no-browser', '--port', '53212', '한 글']
    assert Path(package[1]['TEST_DATA']).is_dir()


def test_portable_uses_adjacent_data_and_preserves_override_and_marker(package):
    stage, environment = package
    assert launch(package, 'start-portable.sh').returncode == 0
    assert record(package)[1] == str(stage / 'data')
    environment['KEIVOTOS_HOME'] = str(stage.parent / 'explicit')
    assert launch(package, 'start-portable.sh').returncode == 0
    assert record(package)[1] == environment['KEIVOTOS_HOME']
    environment.pop('KEIVOTOS_HOME')
    (stage / 'portable.txt').write_text('custom profile')
    assert launch(package, 'start-portable.sh').returncode == 0
    assert record(package)[1] == ''  # The application, not this shell, resolves markers.


def test_diagnostics_do_not_start_the_server_or_require_absent_modules(package):
    result = launch(package, 'start-portable.sh', '--launcher-check')
    assert result.returncode == 0 and 'Linux launcher: ready' in result.stdout
    assert not Path(package[1]['TEST_RECORD']).exists()
    assert not (package[0] / 'gallery-dl').exists()


@pytest.mark.parametrize('name', ['Keivotos', 'ffmpeg'])
def test_missing_execute_permissions_are_actionable(package, name):
    (package[0] / name).chmod(0o644)
    result = launch(package)
    assert result.returncode == 126 and 'Execute permission is missing' in result.stderr
    assert not Path(package[1]['TEST_DATA']).exists()


def test_missing_declared_tool_and_host_resource_fail_before_data_creation(package):
    (package[0] / 'linux-tools.txt').write_text('gallery-dl\n')
    result = launch(package)
    assert result.returncode == 66 and 'Missing bundled program' in result.stderr
    (package[0] / 'linux-tools.txt').write_text('ffmpeg\n')
    package[1]['TEST_RESOURCE_EXIT'] = '1'
    result = launch(package)
    assert result.returncode == 70 and 'resource check failed' in result.stderr
    assert not Path(package[1]['TEST_DATA']).exists()


@pytest.mark.parametrize('entry', ['../outside', '/absolute', '.', 'tool with spaces'])
def test_invalid_tool_list_never_launches_or_writes_data(package, entry):
    (package[0] / 'linux-tools.txt').write_text(entry + '\n')
    assert launch(package).returncode == 66
    assert not Path(package[1]['TEST_RECORD']).exists()


def test_file_collision_preserves_existing_bytes(package):
    target = Path(package[1]['TEST_DATA'])
    target.write_bytes(b'original user file')
    result = launch(package)
    assert result.returncode == 73
    assert target.read_bytes() == b'original user file'


def test_version_forwarding_does_not_require_the_other_tools(package):
    (package[0] / 'ffmpeg').unlink()  # Generated test fixture only.
    result = launch(package, 'start-keivotos.sh', '--version')
    assert result.returncode == 0 and result.stdout == 'fixture version\n'
    assert not Path(package[1]['TEST_DATA']).exists()


def test_optional_desktop_install_is_user_local_and_create_only(package):
    stage, environment = package
    target = Path(environment['XDG_DATA_HOME']) / 'applications/Keivotos.desktop'
    assert launch(package, 'install-desktop.sh').returncode == 0
    original = target.read_bytes()
    assert b'Terminal=true' in original
    icon = stage / '_internal/assets/branding/keivotos/keivotos-icon-256.png'
    assert f'Icon={icon}\n' in original.decode()
    assert icon.is_file()
    result = launch(package, 'install-desktop.sh')
    assert result.returncode == 73 and target.read_bytes() == original
    assert launch(package, 'install-desktop.sh', '--portable').returncode == 0
    portable = target.parent / 'Keivotos-portable.desktop'
    assert portable.is_file()
    assert f'Icon={icon}\n' in portable.read_text()
    assert not Path(environment['TEST_RECORD']).exists()
    assert not Path(environment['TEST_DATA']).exists()


@pytest.mark.skipif(shutil.which('gio') is None, reason='GLib desktop parser unavailable')
def test_desktop_exec_is_parsed_by_glib_without_path_injection(package):
    stage, environment = package
    special = stage.with_name('한 글 " dollar$ tick` slash\\ percent%F')
    stage.rename(special)  # Generated package fixture only.
    package = special, environment
    assert launch(package, 'install-desktop.sh').returncode == 0
    entry = Path(environment['XDG_DATA_HOME']) / 'applications/Keivotos.desktop'
    # Launch the generated fixture without requiring a graphical terminal emulator.
    entry.write_text(entry.read_text().replace('Terminal=true', 'Terminal=false'))
    result = subprocess.run(['gio', 'launch', str(entry)], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 5
    while not Path(environment['TEST_RECORD']).is_file() and time.monotonic() < deadline:
        time.sleep(.05)
    assert record(package) == [str(special), '']
    assert desktop_icon(entry) == str(special / '_internal/assets/branding/keivotos/keivotos-icon-256.png')


def desktop_icon(entry):
    """Read the generated Icon through GLib's real desktop-keyfile parser."""
    import ctypes
    import ctypes.util
    library = ctypes.util.find_library('gio-2.0')
    if not library:
        pytest.skip('GLib desktop parser library unavailable')
    gio = ctypes.CDLL(library)
    glib = ctypes.CDLL(ctypes.util.find_library('glib-2.0'))
    gobject = ctypes.CDLL(ctypes.util.find_library('gobject-2.0'))
    gio.g_desktop_app_info_new_from_filename.argtypes = [ctypes.c_char_p]
    gio.g_desktop_app_info_new_from_filename.restype = ctypes.c_void_p
    gio.g_desktop_app_info_get_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    gio.g_desktop_app_info_get_string.restype = ctypes.c_void_p
    glib.g_free.argtypes = [ctypes.c_void_p]
    glib.g_free.restype = None
    gobject.g_object_unref.argtypes = [ctypes.c_void_p]
    gobject.g_object_unref.restype = None
    info = gio.g_desktop_app_info_new_from_filename(os.fsencode(entry))
    assert info, 'Generated desktop entry is invalid'
    value = None
    try:
        value = gio.g_desktop_app_info_get_string(info, b'Icon')
        assert value
        return ctypes.string_at(value).decode('utf-8')
    finally:
        if value: glib.g_free(value)
        gobject.g_object_unref(info)


@pytest.mark.parametrize('portable', [False, True])
def test_missing_icon_never_creates_a_broken_entry(package, portable):
    stage, environment = package
    icon = stage / '_internal/assets/branding/keivotos/keivotos-icon-256.png'
    icon.unlink()  # Generated package fixture only.
    arguments = ('--portable',) if portable else ()
    result = launch(package, 'install-desktop.sh', *arguments)
    assert result.returncode == 66 and 'desktop icon is missing' in result.stderr
    assert not (Path(environment['XDG_DATA_HOME']) / 'applications').exists()
    assert not Path(environment['TEST_RECORD']).exists()
    assert not Path(environment['TEST_DATA']).exists()


def test_existing_desktop_entry_stays_untouched_even_when_icon_disappears(package):
    stage, environment = package
    assert launch(package, 'install-desktop.sh').returncode == 0
    entry = Path(environment['XDG_DATA_HOME']) / 'applications/Keivotos.desktop'
    original = entry.read_bytes()
    (stage / '_internal/assets/branding/keivotos/keivotos-icon-256.png').unlink()
    result = launch(package, 'install-desktop.sh')
    assert result.returncode == 73 and entry.read_bytes() == original


@pytest.mark.parametrize('portable', [False, True])
def test_glib_reads_the_actual_existing_suite_icon(package, portable):
    stage, environment = package
    arguments = ('--portable',) if portable else ()
    assert launch(package, 'install-desktop.sh', *arguments).returncode == 0
    name = 'Keivotos-portable.desktop' if portable else 'Keivotos.desktop'
    entry = Path(environment['XDG_DATA_HOME']) / 'applications' / name
    assert desktop_icon(entry) == str(stage / '_internal/assets/branding/keivotos/keivotos-icon-256.png')
