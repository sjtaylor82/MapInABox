"""Build and verify Windows metadata used by JAWS Ctrl+Insert+V."""

from pathlib import Path
import ctypes
import ctypes.wintypes
import struct
import sys


REPORTED_FIELDS = (
    "ProductName", "ProductVersion", "FileVersion", "FileDescription",
    "CompanyName", "LegalCopyright",
)


def _require_windows():
    if sys.platform != "win32":
        raise RuntimeError("Windows version resources can only be read on Windows.")


def read_version_strings(path):
    """Read the version strings a screen reader can query from a binary."""
    _require_windows()
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.restype = ctypes.wintypes.DWORD
    size = version.GetFileVersionInfoSizeW(str(Path(path)), None)
    if not size:
        return {}
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(Path(path)), 0, size, buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    value = ctypes.c_void_p()
    length = ctypes.wintypes.UINT()
    if not version.VerQueryValueW(
        buffer, "\\VarFileInfo\\Translation",
        ctypes.byref(value), ctypes.byref(length),
    ) or length.value < 4:
        return {}
    language, codepage = struct.unpack("<HH", ctypes.string_at(value.value, 4))
    strings = {}
    for field in REPORTED_FIELDS:
        query = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{field}"
        if version.VerQueryValueW(
            buffer, query, ctypes.byref(value), ctypes.byref(length)
        ) and length.value:
            text = ctypes.wstring_at(value.value, length.value - 1).strip()
            if text:
                strings[field] = text
    return strings


def announcement(strings):
    name = strings.get("ProductName") or strings.get("FileDescription")
    number = strings.get("ProductVersion") or strings.get("FileVersion")
    return f"{name} version {number}" if name and number else ""


def verify_release_metadata(path, app_name, app_version):
    strings = read_version_strings(path)
    if not strings:
        return ["the binary has no readable version resource"]
    expected = {
        "ProductName": app_name,
        "ProductVersion": app_version,
        "FileVersion": app_version,
        "FileDescription": app_name,
    }
    problems = [
        f"{field} is {strings.get(field, '')!r}, expected {wanted!r}"
        for field, wanted in expected.items()
        if strings.get(field, "") != wanted
    ]
    for field in ("CompanyName", "LegalCopyright"):
        if strings.get(field):
            problems.append(f"{field} would pad the spoken version announcement")
    return problems


def release_targets(app_dir):
    """Files JAWS may identify as owning the focused wxPython window."""
    app_dir = Path(app_dir)
    targets = [app_dir / "MapInABox.exe"]
    targets.extend(sorted((app_dir / "_internal" / "wx").glob("_core*.pyd")))
    return targets


def stamp_version_resource(path, app_name, app_version, file_type=2):
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct,
        VarFileInfo, VarStruct, write_version_info_to_executable,
    )
    parts = tuple(int(part) for part in app_version.split("."))
    if not 1 <= len(parts) <= 4 or any(not 0 <= part <= 65535 for part in parts):
        raise ValueError("APP_VERSION must have 1-4 numeric components")
    numeric = parts + (0,) * (4 - len(parts))
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numeric, prodvers=numeric, mask=0x3F,
                          flags=0, OS=0x40004, fileType=file_type,
                          subtype=0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("ProductName", app_name),
                StringStruct("ProductVersion", app_version),
                StringStruct("FileDescription", app_name),
                StringStruct("FileVersion", app_version),
                StringStruct("InternalName", app_name),
                StringStruct("OriginalFilename", Path(path).name),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )
    write_version_info_to_executable(str(path), info)


def _main(argv):
    if len(argv) == 4 and argv[0] == "--stamp":
        _, target, app_name, app_version = argv
        stamp_version_resource(
            target, app_name, app_version,
            file_type=1 if Path(target).suffix.lower() == ".exe" else 2,
        )
        return 0
    if not argv:
        raise SystemExit("Pass a Windows executable or extension path.")
    strings = read_version_strings(argv[0])
    print(announcement(strings) or "No readable version metadata")
    return 0 if strings else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
