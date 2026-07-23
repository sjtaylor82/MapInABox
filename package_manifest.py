"""Build and compare manifests for release-managed portable application files."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


MANIFEST_FORMAT = 1
MANIFEST_RELATIVE_PATH = "_internal/update-manifest.json"
_EXCLUDED_PATHS = {
    MANIFEST_RELATIVE_PATH.casefold(),
    "_internal/_portable",
    "_internal/_education",
}


def _release_file(relative_path: str) -> bool:
    """Return whether a path belongs to the replaceable application payload."""
    normalized = relative_path.replace("\\", "/").lstrip("/")
    folded = normalized.casefold()
    return (
        folded not in _EXCLUDED_PATHS
        and folded != "data"
        and not folded.startswith("data/")
    )


def hash_file(path: os.PathLike[str] | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(root: os.PathLike[str] | str, version: str,
                   edition: str) -> dict:
    """Return a deterministic manifest for the application tree at *root*."""
    root_path = Path(root)
    files: dict[str, dict[str, int | str]] = {}
    for path in sorted(
            (item for item in root_path.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(root_path).as_posix().casefold()):
        relative = path.relative_to(root_path).as_posix()
        if not _release_file(relative):
            continue
        files[relative] = {
            "size": path.stat().st_size,
            "sha256": hash_file(path),
        }
    return {
        "format": MANIFEST_FORMAT,
        "version": version,
        "edition": edition,
        "files": files,
    }


def write_manifest(root: os.PathLike[str] | str, version: str,
                   edition: str) -> Path:
    """Generate and write the internal update manifest for a distribution."""
    root_path = Path(root)
    destination = root_path / Path(MANIFEST_RELATIVE_PATH)
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(root_path, version, edition)
    destination.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination
