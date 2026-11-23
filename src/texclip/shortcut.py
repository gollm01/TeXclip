"""Create a desktop launcher with the TeXclip icon (Windows .lnk, Linux .desktop)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"
ICON_ICO, ICON_PNG = ASSETS / "texclip.ico", ASSETS / "texclip.png"
APP_ID = "gollm01.TeXclip"          # Windows AppUserModelID: groups the taskbar button under our icon


def launch_command() -> tuple[str, list[str]]:
    """Interpreter and arguments that start the GUI. A source checkout starts ``run_gui.pyw``
    (works without installing); an installed package starts ``-m texclip``."""
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")           # no console window
    launcher = Path(__file__).resolve().parents[2] / "run_gui.pyw"
    return str(exe), [str(launcher)] if launcher.exists() else ["-m", "texclip"]


def set_window_identity(hwnd: int) -> bool:
    """Give one window its own taskbar identity (Windows): AppUserModelID, icon, relaunch command.

    A process-wide ID (SetCurrentProcessExplicitAppUserModelID) is ignored for the Microsoft Store
    Python, which runs as a packaged app, so its taskbar button keeps the Python logo. A
    per-window ID works for every Python. Call it before the window is shown. Returns success."""
    if sys.platform != "win32":
        return False
    import ctypes
    import uuid
    from ctypes import POINTER, byref, c_ubyte, c_ulong, c_ushort, c_void_p, c_wchar_p, wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", c_ulong), ("d2", c_ushort), ("d3", c_ushort), ("d4", c_ubyte * 8)]

    def guid(s):
        u = uuid.UUID(s)
        return GUID(u.time_low, u.time_mid, u.time_hi_version, (c_ubyte * 8)(*u.bytes[8:]))

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", c_ulong)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                    ("val", c_void_p), ("pad", c_void_p)]

    fmt = guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3")          # PKEY_AppUserModel_*
    exe, args = launch_command()
    props = [(2, " ".join(_quote(a) for a in [exe, *args])),   # RelaunchCommand (pinning)
             (4, "TeXclip"),                                    # RelaunchDisplayNameResource
             (3, f"{ICON_ICO},0"),                              # RelaunchIconResource
             (5, APP_ID)]                                       # ID - must be set last
    try:
        ctypes.windll.ole32.CoInitialize(None)
        store = c_void_p()
        if ctypes.windll.shell32.SHGetPropertyStoreForWindow(
                wintypes.HWND(hwnd), byref(guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")), byref(store)):
            return False
        vtbl = ctypes.cast(ctypes.cast(store, POINTER(c_void_p))[0], POINTER(c_void_p))
        set_value = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(PROPERTYKEY),
                                       POINTER(PROPVARIANT))(vtbl[6])
        commit = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p)(vtbl[7])
        release = ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])
        try:
            for pid, value in props:
                s = c_wchar_p(value)                                 # VT_LPWSTR = 31
                set_value(store, byref(PROPERTYKEY(fmt, pid)),
                          byref(PROPVARIANT(31, 0, 0, 0, ctypes.cast(s, c_void_p), None)))
            commit(store)
        finally:
            release(store)
        return True
    except (OSError, AttributeError):
        return False


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def create_shortcut() -> Path:
    """Create the launcher on the desktop and return its path. Raises RuntimeError if unsupported."""
    target, args = launch_command()
    if sys.platform == "win32":
        ps = r"""
$ErrorActionPreference = 'Stop'
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'TeXclip.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $env:TC_TARGET
$s.Arguments = $env:TC_ARGS
$s.WorkingDirectory = $env:USERPROFILE
$s.IconLocation = $env:TC_ICON + ',0'
$s.Description = 'TeXclip - LaTeX equations as vector graphics'
$s.Save()
Write-Output $lnk
"""
        import os
        env = {**os.environ, "TC_TARGET": target, "TC_ARGS": " ".join(_quote(a) for a in args),
               "TC_ICON": str(ICON_ICO)}
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, capture_output=True,
                           text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode:
            raise RuntimeError(r.stderr.strip() or "could not create the shortcut")
        return Path(r.stdout.strip().splitlines()[-1])
    if sys.platform.startswith("linux"):
        entry = ("[Desktop Entry]\nType=Application\nName=TeXclip\n"
                 "Comment=LaTeX equations as vector graphics\n"
                 f"Exec={_quote(target)} {' '.join(_quote(a) for a in args)}\nIcon={ICON_PNG}\n"
                 "Terminal=false\nCategories=Graphics;Science;\n")
        apps = Path.home() / ".local" / "share" / "applications"
        apps.mkdir(parents=True, exist_ok=True)
        (apps / "texclip.desktop").write_text(entry, encoding="utf-8")
        desktop = Path.home() / "Desktop"
        dest = (desktop if desktop.is_dir() else apps) / "texclip.desktop"
        dest.write_text(entry, encoding="utf-8")
        dest.chmod(0o755)
        return dest
    raise RuntimeError("desktop shortcuts are only implemented for Windows and Linux")
