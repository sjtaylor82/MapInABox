param(
    [int]$MapInABoxProcessId,
    [string]$ZipPath,
    [string]$AppDirectory,
    [string]$ExecutablePath,
    [string]$ReadyPath,
    [ValidateSet("pro", "education")]
    [string]$ExpectedEdition
)

$ErrorActionPreference = "Stop"
$manifestRelativePath = "_internal/update-manifest.json"
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("MapInABox-update-" + [guid]::NewGuid())
$payloadDirectory = Join-Path $staging "payload"
$backupDirectory = Join-Path $staging "backup"
$updateLock = Join-Path $AppDirectory ".update-in-progress"
$dataDirectory = Join-Path $AppDirectory "Data"
$updateLog = Join-Path $dataDirectory "update.log"
$installedManifestPath = Join-Path $AppDirectory ($manifestRelativePath.Replace("/", "\"))
$success = $false
$appWasClosed = $false
$changedFiles = New-Object System.Collections.Generic.List[object]
$deletedFiles = New-Object System.Collections.Generic.List[string]
$newDestinations = New-Object System.Collections.Generic.List[string]

New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $payloadDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
$host.UI.RawUI.WindowTitle = "Map in a Box Portable Update"

function Write-UpdateLog([string]$Message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $updateLog -Value "[$stamp] $Message" -Encoding UTF8
}

function Get-FileTable($Manifest) {
    $table = @{}
    if ($null -eq $Manifest -or $null -eq $Manifest.files) {
        return $table
    }
    foreach ($property in $Manifest.files.PSObject.Properties) {
        $table[$property.Name.Replace("\", "/")] = $property.Value
    }
    return $table
}

function Assert-SafeRelativePath([string]$RelativePath) {
    $normalized = $RelativePath.Replace("\", "/")
    if ([string]::IsNullOrWhiteSpace($normalized) -or
            [System.IO.Path]::IsPathRooted($normalized) -or
            $normalized.Contains(":")) {
        throw "The update manifest contains an unsafe path."
    }
    foreach ($part in $normalized.Split("/")) {
        if ([string]::IsNullOrWhiteSpace($part) -or
                $part -eq "." -or $part -eq "..") {
            throw "The update manifest contains an unsafe path."
        }
    }
}

function Backup-ApplicationFile([string]$RelativePath) {
    $windowsPath = $RelativePath.Replace("/", "\")
    $sourcePath = Join-Path $AppDirectory $windowsPath
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        $newDestinations.Add($sourcePath)
        return
    }
    $backupPath = Join-Path $backupDirectory $windowsPath
    $backupParent = Split-Path -Parent $backupPath
    New-Item -ItemType Directory -Path $backupParent -Force | Out-Null
    Copy-Item -LiteralPath $sourcePath -Destination $backupPath -Force
}

try {
    Write-UpdateLog "Portable update started."
    Write-Host "Preparing the Map in a Box portable update."
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $manifestEntry = $archive.Entries | Where-Object {
            $_.FullName.Replace("\", "/").EndsWith("/" + $manifestRelativePath) -or
            $_.FullName.Replace("\", "/") -eq $manifestRelativePath
        } | Select-Object -First 1
        if ($null -eq $manifestEntry) {
            throw "The portable package does not contain an update manifest."
        }

        $manifestReader = New-Object System.IO.StreamReader(
            $manifestEntry.Open(), [System.Text.Encoding]::UTF8)
        try {
            $newManifestJson = $manifestReader.ReadToEnd()
        } finally {
            $manifestReader.Dispose()
        }
        $newManifest = $newManifestJson | ConvertFrom-Json
        if ($newManifest.format -ne 1) {
            throw "The portable package uses an unsupported update manifest."
        }
        if ($newManifest.edition -ne $ExpectedEdition) {
            throw ("The portable package is for the " + $newManifest.edition +
                " edition, but this copy is the $ExpectedEdition edition.")
        }
        $newFiles = Get-FileTable $newManifest
        if ($newFiles.Count -eq 0) {
            throw "The portable package manifest contains no application files."
        }

        $archivePrefix = $manifestEntry.FullName.Substring(
            0, $manifestEntry.FullName.Length - $manifestRelativePath.Length)
        $entryTable = @{}
        foreach ($entry in $archive.Entries) {
            $entryName = $entry.FullName.Replace("\", "/")
            if ($entryName.StartsWith($archivePrefix,
                    [System.StringComparison]::OrdinalIgnoreCase)) {
                $relativeName = $entryName.Substring($archivePrefix.Length)
                if ($relativeName -and -not $relativeName.EndsWith("/")) {
                    $entryTable[$relativeName] = $entry
                }
            }
        }

        $oldManifest = $null
        if (Test-Path -LiteralPath $installedManifestPath -PathType Leaf) {
            try {
                $oldManifest = Get-Content -Raw -LiteralPath $installedManifestPath |
                    ConvertFrom-Json
                if ($oldManifest.format -ne 1 -or
                        $oldManifest.edition -ne $newManifest.edition) {
                    $oldManifest = $null
                }
            } catch {
                Write-UpdateLog "Installed update manifest is invalid; using full replacement."
                $oldManifest = $null
            }
        }
        $oldFiles = Get-FileTable $oldManifest

        foreach ($relativePath in $newFiles.Keys) {
            Assert-SafeRelativePath $relativePath
            if ($newFiles[$relativePath].sha256 -notmatch "^[0-9a-fA-F]{64}$") {
                throw "The update manifest contains an invalid file hash."
            }
            $changed = -not $oldFiles.ContainsKey($relativePath)
            if (-not $changed) {
                $changed = $oldFiles[$relativePath].sha256 -ne
                    $newFiles[$relativePath].sha256
            }
            if (-not $changed) {
                continue
            }
            if (-not $entryTable.ContainsKey($relativePath)) {
                throw "The portable package is missing a manifest file: $relativePath"
            }
            $stagedPath = Join-Path $payloadDirectory ($relativePath.Replace("/", "\"))
            $stagedParent = Split-Path -Parent $stagedPath
            New-Item -ItemType Directory -Path $stagedParent -Force | Out-Null
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile(
                $entryTable[$relativePath], $stagedPath, $true)
            $actualHash = (Get-FileHash -LiteralPath $stagedPath -Algorithm SHA256).Hash
            if ($actualHash -ne $newFiles[$relativePath].sha256) {
                throw "A file in the portable package failed verification: $relativePath"
            }
            $changedFiles.Add([pscustomobject]@{
                RelativePath = $relativePath
                Source = $stagedPath
                Destination = Join-Path $AppDirectory ($relativePath.Replace("/", "\"))
            })
        }
        foreach ($relativePath in $oldFiles.Keys) {
            Assert-SafeRelativePath $relativePath
            if (-not $newFiles.ContainsKey($relativePath)) {
                $deletedFiles.Add($relativePath)
            }
        }
        $stagedManifest = Join-Path $staging "update-manifest.json"
        [System.IO.File]::WriteAllText(
            $stagedManifest, $newManifestJson, [System.Text.Encoding]::UTF8)
    } finally {
        $archive.Dispose()
    }

    $unchanged = $newFiles.Count - $changedFiles.Count
    Write-UpdateLog ("Manifest comparison complete: " + $changedFiles.Count +
        " changed; $unchanged unchanged; " + $deletedFiles.Count + " retired.")
    Set-Content -LiteralPath $ReadyPath -Value "ready" -Encoding ASCII
    Write-Host ("Preparation complete. " + $changedFiles.Count +
        " files will be updated. Closing Map in a Box.")
    Wait-Process -Id $MapInABoxProcessId -ErrorAction SilentlyContinue
    $appWasClosed = $true
    Write-Host "Installing the update. Please keep this window open."

    foreach ($file in $changedFiles) {
        Backup-ApplicationFile $file.RelativePath
    }
    foreach ($relativePath in $deletedFiles) {
        Backup-ApplicationFile $relativePath
    }
    Backup-ApplicationFile $manifestRelativePath

    foreach ($relativePath in $deletedFiles) {
        $obsoletePath = Join-Path $AppDirectory ($relativePath.Replace("/", "\"))
        Remove-Item -LiteralPath $obsoletePath -Force -ErrorAction SilentlyContinue
    }
    foreach ($file in $changedFiles) {
        $destinationParent = Split-Path -Parent $file.Destination
        New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
        $temporaryDestination = $file.Destination + ".update-new"
        Copy-Item -LiteralPath $file.Source -Destination $temporaryDestination -Force
        Move-Item -LiteralPath $temporaryDestination -Destination $file.Destination -Force
    }
    $manifestParent = Split-Path -Parent $installedManifestPath
    New-Item -ItemType Directory -Path $manifestParent -Force | Out-Null
    Copy-Item -LiteralPath $stagedManifest -Destination $installedManifestPath -Force

    $internalMarker = Join-Path $AppDirectory "_internal\_portable"
    $legacyMarker = Join-Path $AppDirectory "portable.flag"
    if ((Test-Path -LiteralPath $internalMarker) -and
            (Test-Path -LiteralPath $legacyMarker)) {
        Remove-Item -LiteralPath $legacyMarker -Force
    }
    $success = $true
    Write-UpdateLog ("Portable update completed: " + $changedFiles.Count +
        " files installed; " + $deletedFiles.Count + " retired.")
    Write-Host "Update complete. Restarting Map in a Box."
} catch {
    Write-UpdateLog ("Portable update failed: " + $_.Exception.Message)
    Write-Host ("The portable update failed: " + $_.Exception.Message)
    if ($appWasClosed) {
        foreach ($destination in $newDestinations) {
            Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $backupDirectory -PathType Container) {
            Get-ChildItem -LiteralPath $backupDirectory -Recurse -Force -File |
                    ForEach-Object {
                $relativePath = $_.FullName.Substring(
                    $backupDirectory.Length).TrimStart("\")
                $restorePath = Join-Path $AppDirectory $relativePath
                $restoreParent = Split-Path -Parent $restorePath
                New-Item -ItemType Directory -Path $restoreParent -Force | Out-Null
                Copy-Item -LiteralPath $_.FullName -Destination $restorePath -Force
            }
            Write-UpdateLog "Previous application files restored after update failure."
        }
    }
    $env:MIAB_PORTABLE_UPDATE_FAILED = $updateLog
    if (-not (Test-Path -LiteralPath $ReadyPath)) {
        Set-Content -LiteralPath $ReadyPath -Value "error" -Encoding ASCII
    }
} finally {
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
