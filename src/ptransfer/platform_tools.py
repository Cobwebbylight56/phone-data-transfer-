"""Locating (and if needed fetching) adb + fastboot.

The app never ships Google's binaries in the repo. It looks for them in this
order:

1. ``PTRANSFER_ADB`` / ``PTRANSFER_FASTBOOT`` environment overrides
2. a ``platform-tools`` folder next to the executable / repo root
3. the user data dir (where :func:`download_platform_tools` puts them)
4. whatever is on PATH
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import stat
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DOWNLOAD_URLS = {
    "Windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "Linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
    "Darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
}


def is_windows() -> bool:
    return os.name == "nt"


def exe(name: str) -> str:
    return f"{name}.exe" if is_windows() else name


def app_dir() -> Path:
    """Directory the app is running from (PyInstaller aware)."""
    if getattr(sys, "frozen", False):  # pragma: no cover - packaged only
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[2]


def user_data_dir() -> Path:
    if is_windows():  # pragma: no cover - platform dependent
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "PhoneDataTransfer"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Tools:
    adb: str
    fastboot: str
    source: str = "path"

    @property
    def complete(self) -> bool:
        return bool(self.adb) and bool(self.fastboot)


def _candidate_dirs() -> list[Path]:
    return [
        app_dir() / "platform-tools",
        app_dir(),
        user_data_dir() / "platform-tools",
    ]


def find_tool(name: str, env_var: str) -> tuple[str | None, str]:
    override = os.environ.get(env_var)
    if override and Path(override).exists():
        return override, "env"

    for d in _candidate_dirs():
        p = d / exe(name)
        if p.exists():
            return str(p), "bundled"

    found = shutil.which(name)
    if found:
        return found, "path"
    return None, "missing"


def discover() -> Tools:
    adb, adb_src = find_tool("adb", "PTRANSFER_ADB")
    fastboot, fb_src = find_tool("fastboot", "PTRANSFER_FASTBOOT")
    return Tools(adb or "", fastboot or "", adb_src if adb else fb_src)


def download_platform_tools(dest: Path | None = None, progress=None) -> Path:
    """Fetch Google's platform-tools zip and unpack it.

    Returns the directory containing adb/fastboot. Network access required;
    callers should surface failures to the user rather than swallowing them.
    """
    import urllib.request

    system = platform.system()
    url = DOWNLOAD_URLS.get(system)
    if not url:
        raise RuntimeError(f"No platform-tools download known for {system}")

    dest = dest or user_data_dir()
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest / "platform-tools.zip"

    def _hook(count, block, total):
        if progress and total > 0:
            progress(min(1.0, count * block / total))

    log.info("Downloading platform-tools from %s", url)
    urllib.request.urlretrieve(url, zip_path, reporthook=_hook)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    zip_path.unlink(missing_ok=True)

    out = dest / "platform-tools"
    if not is_windows():
        for name in ("adb", "fastboot"):
            p = out / name
            if p.exists():
                p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return out
