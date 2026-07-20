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
import time
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
        """Start a detached helper that applies changed files and restarts."""
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

        update_lock = os.path.join(APP_DIR, ".update-in-progress")
        with open(update_lock, "w", encoding="utf-8") as lock_file:
            lock_file.write("A portable update is being installed.\n")

        script_path = os.path.join(
            tempfile.gettempdir(), f"MapInABox-portable-update-{os.getpid()}.ps1")
        ready_path = os.path.join(
            tempfile.gettempdir(), f"MapInABox-portable-ready-{os.getpid()}.txt")
        script = r'''param(
    [int]$MapInABoxProcessId,
    [string]$ZipPath,
    [string]$AppDirectory,
    [string]$ExecutablePath,
    [string]$ReadyPath
)
$ErrorActionPreference = "Stop"
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("MapInABox-update-" + [guid]::NewGuid())
$updateLock = Join-Path $AppDirectory ".update-in-progress"
$dataDirectory = Join-Path $AppDirectory "Data"
$updateLog = Join-Path $dataDirectory "update.log"
$success = $false
$appWasClosed = $false
$soundPlayer = $null
$destinationSoundDirectory = $null
$soundBackupDirectory = $null
New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null
function Write-UpdateLog([string]$Message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $updateLog -Value "[$stamp] $Message" -Encoding UTF8
}
try {
    Write-UpdateLog "Portable update started."
    $processingSound = Join-Path $env:WINDIR "Media\Windows Background.wav"
    if (Test-Path -LiteralPath $processingSound) {
        $soundPlayer = New-Object System.Media.SoundPlayer $processingSound
        $soundPlayer.PlayLooping()
    } else {
        [System.Media.SystemSounds]::Asterisk.Play()
    }
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $staging -Force
    $source = Join-Path $staging "MapInABox"
    if (-not (Test-Path -LiteralPath $source)) { $source = $staging }
    $sourceSoundDirectory = Join-Path $source "_internal\sounds"
    $soundRelativePath = "_internal\sounds"
    if (-not (Test-Path -LiteralPath $sourceSoundDirectory -PathType Container)) {
        $sourceSoundDirectory = Join-Path $source "sounds"
        $soundRelativePath = "sounds"
    }
    if (Test-Path -LiteralPath $sourceSoundDirectory -PathType Container) {
        $destinationSoundDirectory = Join-Path $AppDirectory $soundRelativePath
        $soundBackupDirectory = $destinationSoundDirectory + ".update-old"
    }
    $changedFiles = New-Object System.Collections.Generic.List[object]
    $unchanged = 0
    Get-ChildItem -LiteralPath $source -Recurse -Force -File | ForEach-Object {
        $relativePath = $_.FullName.Substring($source.Length).TrimStart('\')
        $destination = Join-Path $AppDirectory $relativePath
        $isBundledSound = ($destinationSoundDirectory -ne $null) -and
            $_.FullName.StartsWith(
                $sourceSoundDirectory + [System.IO.Path]::DirectorySeparatorChar,
                [System.StringComparison]::OrdinalIgnoreCase)
        $different = $isBundledSound -or
            -not (Test-Path -LiteralPath $destination -PathType Leaf)
        if (-not $different) {
            $destinationItem = Get-Item -LiteralPath $destination
            if ($destinationItem.Length -ne $_.Length) {
                $different = $true
            } else {
                $sourceHash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
                $destinationHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
                $different = $sourceHash -ne $destinationHash
            }
        }
        if ($different) {
            $changedFiles.Add([pscustomobject]@{
                Source = $_.FullName
                Destination = $destination
            })
        } else {
            $unchanged++
        }
    }
    Write-UpdateLog ("Preparation complete: " + $changedFiles.Count +
        " changed; $unchanged unchanged. Waiting for the app to close.")
    Set-Content -LiteralPath $ReadyPath -Value "ready" -Encoding ASCII
    Wait-Process -Id $MapInABoxProcessId -ErrorAction SilentlyContinue
    $appWasClosed = $true
    if ($destinationSoundDirectory) {
        Remove-Item -LiteralPath $soundBackupDirectory -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $destinationSoundDirectory) {
            Move-Item -LiteralPath $destinationSoundDirectory -Destination $soundBackupDirectory
        }
        Write-UpdateLog "Previous bundled sounds moved aside for complete replacement."
    }
    foreach ($file in $changedFiles) {
        $destinationDirectory = Split-Path -Parent $file.Destination
        New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
        $temporaryDestination = $file.Destination + ".update-new"
        Copy-Item -LiteralPath $file.Source -Destination $temporaryDestination -Force
        Move-Item -LiteralPath $temporaryDestination -Destination $file.Destination -Force
    }
    Write-UpdateLog ("Files applied: " + $changedFiles.Count +
        " changed; $unchanged unchanged.")
    $internalMarker = Join-Path $AppDirectory "_internal\_portable"
    $legacyMarker = Join-Path $AppDirectory "portable.flag"
    if ((Test-Path -LiteralPath $internalMarker) -and
            (Test-Path -LiteralPath $legacyMarker)) {
        Remove-Item -LiteralPath $legacyMarker -Force
    }
    if ($soundBackupDirectory) {
        Remove-Item -LiteralPath $soundBackupDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
    $success = $true
    Write-UpdateLog "Portable update completed successfully."
} catch {
    Write-UpdateLog ("Portable update failed: " + $_.Exception.Message)
    if ($soundBackupDirectory -and
            (Test-Path -LiteralPath $soundBackupDirectory -PathType Container)) {
        Remove-Item -LiteralPath $destinationSoundDirectory -Recurse -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $soundBackupDirectory -Destination $destinationSoundDirectory -Force
        Write-UpdateLog "Previous bundled sounds restored after update failure."
    }
    $env:MIAB_PORTABLE_UPDATE_FAILED = $updateLog
    if (-not (Test-Path -LiteralPath $ReadyPath)) {
        Set-Content -LiteralPath $ReadyPath -Value "error" -Encoding ASCII
    }
} finally {
    if ($soundPlayer) { $soundPlayer.Stop() }
    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    if ($success) {
        Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $updateLock -Force -ErrorAction SilentlyContinue
    if ($appWasClosed -and (Test-Path -LiteralPath $ExecutablePath)) {
        Start-Process -FilePath $ExecutablePath
    }
    if ($success) {
        Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    }
}
'''
        with open(script_path, "w", encoding="utf-8-sig") as script_file:
            script_file.write(script)

        executable_path = os.path.join(APP_DIR, "MapInABox.exe")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            helper = subprocess.Popen(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-WindowStyle", "Hidden", "-File", script_path,
                    "-MapInABoxProcessId", str(os.getpid()),
                    "-ZipPath", zip_path,
                    "-AppDirectory", APP_DIR,
                    "-ExecutablePath", executable_path,
                    "-ReadyPath", ready_path,
                ],
                creationflags=creation_flags,
                close_fds=True,
            )
        except Exception:
            try:
                os.remove(update_lock)
            except OSError:
                pass
            raise
        deadline = time.monotonic() + 1800
        while not os.path.isfile(ready_path):
            if helper.poll() is not None:
                raise RuntimeError("Portable update helper stopped during preparation")
            if time.monotonic() >= deadline:
                raise TimeoutError("Portable update preparation timed out")
            time.sleep(0.2)
        with open(ready_path, "r", encoding="ascii", errors="replace") as ready_file:
            ready_status = ready_file.read().strip().lower()
        try:
            os.remove(ready_path)
        except OSError:
            pass
        if ready_status != "ready":
            raise RuntimeError("Portable update preparation failed; see Data\\update.log")
