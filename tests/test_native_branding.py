"""Native identity/icon checks; all launches and profiles are disposable."""
from __future__ import annotations

import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def branding(tmp_path, monkeypatch):
    monkeypatch.setenv("KEIVOTOS_HOME", str(tmp_path / "home"))
    spec = importlib.util.spec_from_file_location("native_branding_app", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def windows(branding, monkeypatch):
    kernel = SimpleNamespace(SetConsoleTitleW=Mock(return_value=1), GetConsoleWindow=Mock(return_value=123))
    user = SimpleNamespace(LoadImageW=Mock(side_effect=[0x100000001, 0x100000002]), SendMessageW=Mock(return_value=0))
    shell = SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=Mock(return_value=0))
    libraries = {"kernel32": kernel, "user32": user, "shell32": shell}
    load = Mock(side_effect=lambda name, **kwargs: libraries[name])
    monkeypatch.setattr(branding.ctypes, "WinDLL", load, raising=False)
    monkeypatch.setattr(branding.sys, "platform", "win32")
    return branding, kernel, user, shell, load


def test_branding_keeps_pointer_sized_icons_and_reuses_two_handles(windows):
    app, kernel, user, shell, load = windows
    app._set_console_branding()
    app._set_console_branding()
    assert shell.SetCurrentProcessExplicitAppUserModelID.call_args.args == (app.WINDOWS_APP_ID,)
    assert shell.SetCurrentProcessExplicitAppUserModelID.argtypes == [ctypes.c_wchar_p]
    assert shell.SetCurrentProcessExplicitAppUserModelID.restype is ctypes.c_int32
    assert kernel.GetConsoleWindow.restype is ctypes.c_void_p
    assert user.LoadImageW.restype is ctypes.c_void_p
    assert user.SendMessageW.argtypes == [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
    assert user.SendMessageW.restype is ctypes.c_ssize_t
    assert kernel.SetConsoleTitleW.call_args.args == (app.WEB_TITLE,)
    assert user.LoadImageW.call_count == 2
    assert [call.args for call in user.SendMessageW.call_args_list] == [
        (123, 0x80, 1, 0x100000001), (123, 0x80, 0, 0x100000002),
        (123, 0x80, 1, 0x100000001), (123, 0x80, 0, 0x100000002),
    ]
    assert app._console_icons == {32: 0x100000001, 16: 0x100000002}
    assert all(call.kwargs == {"use_last_error": True} for call in load.call_args_list)


@pytest.mark.parametrize("reason", ["no-window", "missing-icon"])
def test_identity_and_title_still_apply_without_an_icon_target(windows, monkeypatch, tmp_path, reason):
    app, kernel, user, shell, _ = windows
    if reason == "no-window": kernel.GetConsoleWindow.return_value = None
    else: monkeypatch.setattr(app, "CONSOLE_ICON_PATH", tmp_path / "missing.ico")
    app._set_console_branding()
    shell.SetCurrentProcessExplicitAppUserModelID.assert_called_once_with(app.WINDOWS_APP_ID)
    kernel.SetConsoleTitleW.assert_called_once_with(app.WEB_TITLE)
    user.LoadImageW.assert_not_called()
    user.SendMessageW.assert_not_called()


def test_failed_icon_load_is_not_sent_or_cached(windows):
    app, _, user, _, _ = windows
    user.LoadImageW.side_effect = [None, None]
    app._set_console_branding()
    user.SendMessageW.assert_not_called()
    assert app._console_icons == {}


def test_shell_identity_failure_does_not_remove_existing_console_branding(windows):
    app, kernel, user, shell, _ = windows
    shell.SetCurrentProcessExplicitAppUserModelID.side_effect = OSError("fixture unsupported")
    app._set_console_branding()
    kernel.SetConsoleTitleW.assert_called_once_with(app.WEB_TITLE)
    assert user.SendMessageW.call_count == 2


@pytest.mark.parametrize("error", [OSError("fixture unavailable"), AttributeError("fixture unavailable")])
def test_unavailable_windows_api_is_harmless(windows, error):
    app, _, _, _, load = windows
    load.side_effect = error
    app._set_console_branding()


def test_other_platform_never_loads_windows_libraries(branding, monkeypatch):
    monkeypatch.setattr(branding.sys, "platform", "linux")
    load = Mock(side_effect=AssertionError("Unexpected Windows API"))
    monkeypatch.setattr(branding.ctypes, "WinDLL", load, raising=False)
    branding._set_console_branding()
    load.assert_not_called()


def test_normal_start_brands_before_configuration_can_present_ui(branding, monkeypatch):
    import module_registry
    order = []
    monkeypatch.setattr(module_registry, "dispatch_helper", lambda *a, **kw: None)
    monkeypatch.setattr(branding, "_set_console_branding", lambda: order.append("branding"))
    def stop(**kwargs):
        order.append("configuration")
        assert kwargs == {"migrate_legacy_home": True}
        raise RuntimeError("fixture stop before server")
    monkeypatch.setattr(branding, "_load_configuration", stop)
    with pytest.raises(RuntimeError, match="fixture stop"):
        branding.main([])
    assert order == ["branding", "configuration"]


@pytest.mark.parametrize("arguments,helper", [(["--version"], None), (["--portable-check"], None),
    (["--folder-picker"], None), (["--module-worker"], 17)])
def test_diagnostic_and_helper_paths_do_not_brand_the_calling_shell(branding, monkeypatch, arguments, helper):
    import module_registry
    monkeypatch.setattr(module_registry, "dispatch_helper", lambda *a, **kw: helper)
    brand = Mock(side_effect=AssertionError("Diagnostic/helper must not brand a shell"))
    monkeypatch.setattr(branding, "_set_console_branding", brand)
    monkeypatch.setattr(branding, "_load_configuration", lambda **kwargs: object())
    monkeypatch.setattr(branding, "_portable_check", lambda *_: 0)
    monkeypatch.setattr(branding, "_run_helper", lambda *_: 0)
    assert branding.main(arguments) == (17 if helper is not None else 0)
    brand.assert_not_called()


def test_existing_logo_assets_decode_at_the_required_native_sizes():
    icon = ROOT / "assets/branding/keivotos/keivotos.ico"
    assert icon.read_bytes() == (ROOT / "packaging/windows/assets/keivotos.ico").read_bytes()
    with Image.open(icon) as image:
        assert {(16, 16), (32, 32), (256, 256)} <= image.ico.sizes()
        for size in (16, 32):
            decoded = image.ico.getimage((size, size))
            assert decoded.size == (size, size) and decoded.getbbox()
    with Image.open(ROOT / "assets/branding/keivotos/keivotos-icon-256.png") as image:
        image.load()
        assert image.size == (256, 256) and image.getbbox()


NATIVE_CHILD = r"""
import ctypes,json,sys
from ctypes import wintypes
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import app
app._set_console_branding()
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
user=ctypes.WinDLL('user32',use_last_error=True)
shell=ctypes.WinDLL('shell32',use_last_error=True)
ole=ctypes.WinDLL('ole32',use_last_error=True)
gdi=ctypes.WinDLL('gdi32',use_last_error=True)
shell.GetCurrentProcessExplicitAppUserModelID.argtypes=[ctypes.POINTER(ctypes.c_void_p)]
shell.GetCurrentProcessExplicitAppUserModelID.restype=ctypes.c_int32
ole.CoTaskMemFree.argtypes=[ctypes.c_void_p];ole.CoTaskMemFree.restype=None
value=ctypes.c_void_p()
assert shell.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(value))==0
try:identity=ctypes.wstring_at(value)
finally:ole.CoTaskMemFree(value)
kernel.GetConsoleWindow.argtypes=[];kernel.GetConsoleWindow.restype=ctypes.c_void_p
kernel.GetConsoleTitleW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint];kernel.GetConsoleTitleW.restype=ctypes.c_uint
window=kernel.GetConsoleWindow();title=ctypes.create_unicode_buffer(256)
kernel.GetConsoleTitleW(title,len(title))
user.SendMessageTimeoutW.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_size_t,ctypes.c_ssize_t,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_size_t)]
user.SendMessageTimeoutW.restype=ctypes.c_ssize_t
class ICONINFO(ctypes.Structure):
 _fields_=[('fIcon',wintypes.BOOL),('xHotspot',wintypes.DWORD),('yHotspot',wintypes.DWORD),('hbmMask',ctypes.c_void_p),('hbmColor',ctypes.c_void_p)]
class BITMAP(ctypes.Structure):
 _fields_=[('bmType',wintypes.LONG),('bmWidth',wintypes.LONG),('bmHeight',wintypes.LONG),('bmWidthBytes',wintypes.LONG),('bmPlanes',wintypes.WORD),('bmBitsPixel',wintypes.WORD),('bmBits',ctypes.c_void_p)]
user.GetIconInfo.argtypes=[ctypes.c_void_p,ctypes.POINTER(ICONINFO)];user.GetIconInfo.restype=wintypes.BOOL
gdi.GetObjectW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p];gdi.GetObjectW.restype=ctypes.c_int
gdi.DeleteObject.argtypes=[ctypes.c_void_p];gdi.DeleteObject.restype=wintypes.BOOL
sizes={};readback={}
for kind,size in ((1,32),(0,16)):
 handle=app._console_icons.get(size)
 assert handle,'Native LoadImageW failed'
 info=ICONINFO();assert user.GetIconInfo(handle,ctypes.byref(info))
 try:
  bitmap=BITMAP();assert gdi.GetObjectW(info.hbmColor,ctypes.sizeof(bitmap),ctypes.byref(bitmap))
  sizes[str(size)]=[bitmap.bmWidth,bitmap.bmHeight]
 finally:
  if info.hbmMask:gdi.DeleteObject(info.hbmMask)
  if info.hbmColor:gdi.DeleteObject(info.hbmColor)
 result=ctypes.c_size_t()
 completed=user.SendMessageTimeoutW(window,0x7f,kind,0,2,1000,ctypes.byref(result)) if window else 0
 readback[str(size)]=bool(completed and result.value==handle)
print(json.dumps({'identity':identity,'title':title.value,'window_present':bool(window),'sizes':sizes,'icon_readback':readback,'handles':app._console_icons,'expected_home':str(Path(__import__('os').environ['KEIVOTOS_HOME']))}))
"""


@pytest.fixture(scope="module")
def native_result(tmp_path_factory):
    if sys.platform != "win32":
        pytest.skip("Native Windows identity and console readback")
    directory = tmp_path_factory.mktemp("native-branding")
    environment = {**os.environ, "KEIVOTOS_HOME": str(directory / "home"), "PYTHONUTF8": "1"}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    result = subprocess.run([sys.executable, "-c", NATIVE_CHILD, str(ROOT)], cwd=ROOT,
        env=environment, startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE,
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(result.stdout)
    assert record["expected_home"] == environment["KEIVOTOS_HOME"]
    if os.environ.get("KEIVOTOS_NATIVE_BRANDING_REPORT"):
        Path(os.environ["KEIVOTOS_NATIVE_BRANDING_REPORT"]).write_text(json.dumps(record, indent=2))
    return record


def test_native_windows_application_identity_and_icon_sizes(native_result):
    assert native_result["identity"] == "Keivotos.Desktop"
    assert native_result["title"] == "Keivotos"
    assert native_result["sizes"] == {"32": [32, 32], "16": [16, 16]}


def test_native_windows_console_reports_the_applied_icon_handles(native_result):
    if not native_result["window_present"] or not all(native_result["icon_readback"].values()):
        pytest.skip("Host does not expose classic console icon readback; see native branding report")
    assert native_result["icon_readback"] == {"32": True, "16": True}
