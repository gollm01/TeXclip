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
