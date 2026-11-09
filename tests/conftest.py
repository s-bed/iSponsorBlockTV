"""Run Home Assistant tests against the packaged integration."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from tools.build_home_assistant import build

_staging: TemporaryDirectory[str]


def pytest_sessionstart() -> None:
    """Build and unpack the same files users install."""
    global _staging
    _staging = TemporaryDirectory(prefix="isponsorblocktv-tests-")
    folder = Path(_staging.name)
    archive = folder / "integration.zip"
    build(archive)
    with ZipFile(archive) as package:
        package.extractall(folder)
    sys.path.insert(0, str(folder))


def pytest_sessionfinish() -> None:
    """Remove the packaged test copy."""
    sys.path.remove(_staging.name)
    _staging.cleanup()
