"""Build a self-contained Home Assistant custom integration ZIP."""

from __future__ import annotations

import json
from argparse import ArgumentParser
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = Path("custom_components/isponsorblocktv")
SOURCE = ROOT / "src/iSponsorBlockTV"
SHARED_MODULES = (
    "api_helpers.py",
    "conditional_ttl_cache.py",
    "constants.py",
    "pairing.py",
    "playback.py",
    "ytlounge.py",
)


def build(output: Path, *, version: str | None = None, hacs: bool = False) -> None:
    """Bundle the component with its shared runtime, without the CLI."""
    component_files = {
        path.relative_to(ROOT): path.read_bytes()
        for path in (ROOT / COMPONENT).rglob("*")
        if path.is_file() and path.suffix in {".py", ".json"}
    }
    core_files = {
        COMPONENT / "core" / module: (SOURCE / module).read_bytes() for module in SHARED_MODULES
    }
    files = {
        **component_files,
        **core_files,
        COMPONENT / "core/__init__.py": b'"""Shared iSponsorBlockTV runtime."""\n',
        COMPONENT / "LICENSE.md": (ROOT / "LICENSE.md").read_bytes(),
    }
    if version is not None:
        manifest_path = COMPONENT / "manifest.json"
        manifest = json.loads(files[manifest_path])
        manifest["version"] = version
        files[manifest_path] = (json.dumps(manifest, indent=2) + "\n").encode()

    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w") as archive:
        for path, contents in sorted(files.items()):
            if hacs:
                path = path.relative_to(COMPONENT)
            member = ZipInfo(path.as_posix())
            member.create_system = 3  # Unix permissions on every platform
            member.external_attr = 0o100644 << 16
            archive.writestr(member, contents, compress_type=ZIP_DEFLATED)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist/isponsorblocktv-home-assistant.zip",
        help="Destination ZIP file",
    )
    parser.add_argument("--version", help="Version to write into the bundled manifest")
    parser.add_argument("--hacs", action="store_true", help="Put integration files at the ZIP root")
    args = parser.parse_args()
    build(args.output, version=args.version, hacs=args.hacs)
    print(args.output.resolve())
