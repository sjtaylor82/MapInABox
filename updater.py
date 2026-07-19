"""updater.py — Background update checker for Map in a Box.

Checks the GitHub releases API on startup and notifies the app if a newer
version is available.  All network activity runs in a daemon thread so it
never blocks startup or the UI.

Usage
-----
    from updater import UpdateChecker

    checker = UpdateChecker(
        current_version = APP_VERSION,          # e.g. "1.0"
        repo            = "sjtaylor82/MapInABox",
        on_update_found = callback,             # called on the main thread
    )
    checker.start()   # non-blocking

    # Later, if the user confirms:
    checker.download_and_install()
"""

import json
import os
import platform
import re
import sys
import tempfile
import threading
import urllib.request
from logging_utils import miab_log
from app_paths import APP_DIR, PORTABLE_MODE


GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
REQUEST_TIMEOUT = 8   # seconds — fail silently if slow


# ── Version helpers ────────────────────────────────────────────────────────────

def _parse_version(v: str) -> tuple[int, ...]:
    """Turn '1.0', 'v1.2.3', '2.0.1' into (1, 0), (1, 2, 3), (2, 0, 1)."""
    v = v.lstrip("v").strip()
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts) if parts else (0,)


def _is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


# ── Asset selection ────────────────────────────────────────────────────────────

def _pick_asset(assets: list[dict], portable: bool = PORTABLE_MODE,
                platform: str = sys.platform) -> dict | None:
    """Return the most appropriate release asset for the current platform."""
    if platform == "darwin":
        # Prefer a file with 'macos' or 'mac' in the name
        for a in assets:
            if re.search(r"mac(os)?", a["name"], re.IGNORECASE):
                return a
    else:
        if portable:
            # A portable copy must never hand off to the Windows installer.
            for a in assets:
                name = a["name"].lower()
                if "windows-portable" in name and name.endswith(".zip"):
                    return a
            return None
        # Installed Windows build — prefer the installer.
        for a in assets:
            if a["name"].lower().endswith(".exe"):
                return a
    return None


# ── Main class ─────────────────────────────────────────────────────────────────

class UpdateChecker:
    """Check for updates in a background thread and notify the app."""

    def __init__(self, current_version: str, repo: str, on_update_found,
                 on_no_update=None, on_check_error=None):
        self.current_version = current_version
        self.repo            = repo
        self.on_update_found = on_update_found  # callable(latest_version: str)
        self.on_no_update = on_no_update
        self.on_check_error = on_check_error
        self.latest_version: str       = ""
        self._asset:         dict | None = None
        self.downloaded_asset_path: str = ""
        self.portable_restart_scheduled = False
        self._lock           = threading.Lock()

    def start(self) -> None:
        """Start the background check — returns immediately."""
        t = threading.Thread(target=self._check, daemon=True)
        t.start()

    def _check(self) -> None:
        try:
            url = GITHUB_API.format(repo=self.repo)
            req = urllib.request.Request(
                url, headers={"User-Agent": f"MapInABox/{self.current_version}"}
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read())

            tag    = data.get("tag_name", "")
            assets = data.get("assets", [])

            if not _is_newer(tag, self.current_version):
                if self.on_no_update:
                    import wx
                    wx.CallAfter(self.on_no_update)
                return

            asset = _pick_asset(assets)
            with self._lock:
                self.latest_version = tag.lstrip("v")
                self._asset         = asset

            # Fire callback on the calling thread via wx.CallAfter so it's
            # safe to touch the UI.
            import wx
            wx.CallAfter(self.on_update_found, self.latest_version)

        except Exception as e:
            # Never raise — update check should be completely silent on failure
            miab_log("errors", f"[Updater] Check failed (non-fatal): {e}", getattr(self, "settings", None))
            if self.on_check_error:
                import wx
                wx.CallAfter(self.on_check_error)

    def download_and_install(self, progress_cb=None) -> bool:
        """Download the release asset and launch it.  Returns False on error.

        On installed Windows: downloads and launches the installer.
        On portable Windows: downloads the portable ZIP and starts a hidden
        helper which waits for this process to exit, replaces the program
        files while preserving Data, and restarts the app.
        On macOS:   opens the release page in the browser (replacing a running
                    .app is not safe to do in-process).

        This method does blocking network I/O (potentially large files) and
        is meant to be called from a background thread — never call it
        directly on the wx main thread, or the UI will look like it has
        frozen ("Not Responding") for the whole download.

        progress_cb, if given, is called as progress_cb(percent: int) from
        whatever thread this method is running on — the caller is
        responsible for hopping back to the main thread (e.g. wx.CallAfter)
        before touching any UI from inside it.
        """
        import webbrowser

        with self._lock:
            asset   = self._asset
            version = self.latest_version

        if sys.platform == "darwin":
            # Safe macOS path: open the releases page, let the user do it
            webbrowser.open(
                f"https://github.com/{self.repo}/releases/tag/v{version}"
            )
            return True

        if not asset:
            # No matching asset found — fall back to the release page. Portable
            # mode deliberately does not fall back to an installer executable.
            webbrowser.open(f"https://github.com/{self.repo}/releases/latest")
            return True

        url      = asset["browser_download_url"]
        filename = asset["name"]
        dest     = os.path.join(tempfile.gettempdir(), filename)

        def _reporthook(block_num, block_size, total_size):
            if not progress_cb or total_size <= 0:
                return
            pct = min(100, int(block_num * block_size * 100 / total_size))
            try:
                progress_cb(pct)
            except Exception:
                pass

        try:
            miab_log("verbose", f"[Updater] Downloading {url} ...", getattr(self, "settings", None))
            urllib.request.urlretrieve(url, dest, reporthook=_reporthook)
            self.downloaded_asset_path = dest
            if PORTABLE_MODE:
                self._schedule_portable_replacement(dest)
                self.portable_restart_scheduled = True
            else:
                miab_log("verbose", f"[Updater] Launching {dest}", getattr(self, "settings", None))
                os.startfile(dest)   # Installed Windows build: launch installer.
            return True
        except Exception as e:
            miab_log("errors", f"[Updater] Download/launch failed: {e}", getattr(self, "settings", None))
            return False

    @staticmethod
    def _schedule_portable_replacement(zip_path: str) -> None:
        """Start a detached PowerShell helper to replace and restart the app."""
        import subprocess

        # Confirm the portable folder is writable before closing the app.
        probe = os.path.join(APP_DIR, ".update-write-test")
        try:
            with open(probe, "w", encoding="utf-8") as probe_file:
                probe_file.write("ok")
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass

        script_path = os.path.join(
            tempfile.gettempdir(), f"MapInABox-portable-update-{os.getpid()}.ps1")
        script = r'''param(
    [int]$MapInABoxProcessId,
    [string]$ZipPath,
    [string]$AppDirectory,
    [string]$ExecutablePath
)
$ErrorActionPreference = "Stop"
Wait-Process -Id $MapInABoxProcessId -ErrorAction SilentlyContinue
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("MapInABox-update-" + [guid]::NewGuid())
try {
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $staging -Force
    $source = Join-Path $staging "MapInABox"
    if (-not (Test-Path -LiteralPath $source)) { $source = $staging }
    Get-ChildItem -LiteralPath $source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $AppDirectory -Recurse -Force
    }
    Start-Process -FilePath $ExecutablePath
} finally {
    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
'''
        with open(script_path, "w", encoding="utf-8-sig") as script_file:
            script_file.write(script)

        executable_path = os.path.join(APP_DIR, "MapInABox.exe")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden", "-File", script_path,
                "-MapInABoxProcessId", str(os.getpid()),
                "-ZipPath", zip_path,
                "-AppDirectory", APP_DIR,
                "-ExecutablePath", executable_path,
            ],
            creationflags=creation_flags,
            close_fds=True,
        )
