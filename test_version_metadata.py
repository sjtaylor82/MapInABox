import ast
from pathlib import Path

from version_resource import announcement, release_targets


def _app_constants():
    values = {}
    tree = ast.parse(Path("core.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in {"APP_NAME", "APP_VERSION"}:
                values[target.id] = ast.literal_eval(node.value)
    return values


def test_screen_reader_announcement_is_name_and_version_only():
    constants = _app_constants()
    assert announcement({
        "ProductName": constants["APP_NAME"],
        "ProductVersion": constants["APP_VERSION"],
    }) == f'{constants["APP_NAME"]} version {constants["APP_VERSION"]}'


def test_release_targets_include_executable_and_window_owning_wx_module():
    targets = release_targets(Path("dist") / "MapInABox")
    assert targets[0].name == "MapInABox.exe"
    assert any(path.name.startswith("_core") and path.suffix == ".pyd"
               for path in targets)


def test_spec_reads_release_identity_from_core_instead_of_hard_coding_it():
    spec = Path("MapInABox.spec").read_text(encoding="utf-8")
    assert "constants['APP_NAME']" in spec
    assert "constants['APP_VERSION']" in spec
    assert "2026.9.2" not in spec
