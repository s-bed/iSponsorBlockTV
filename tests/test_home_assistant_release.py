"""Test release version selection and ZIP layouts without publishing."""

import json
from zipfile import ZipFile

import pytest

from tools.build_home_assistant import build
from tools.home_assistant_version import git, release_version


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "--initial-branch=main")
    git(tmp_path, "config", "user.email", "ci@example.com")
    git(tmp_path, "config", "user.name", "CI Test")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.11.0"\n')
    git(tmp_path, "add", "pyproject.toml")
    git(tmp_path, "commit", "-qm", "Initial standalone version")
    git(tmp_path, "tag", "v2.11.0")
    return tmp_path


def test_initial_versions_and_preview_reruns(repo):
    assert release_version(repo) == "2.12.0"
    git(repo, "switch", "-qc", "codex/preview")
    git(repo, "commit", "--allow-empty", "-qm", "feat: Integration")
    assert release_version(repo, preview=23) == "2.12.0-beta.23"
    git(repo, "tag", "2.12.0-beta.23")

    git(repo, "switch", "-q", "main")
    git(repo, "commit", "--allow-empty", "-qm", "feat: Merge another integration change")
    git(repo, "tag", "2.12.0")
    git(repo, "switch", "-q", "codex/preview")

    assert release_version(repo, preview=23) == "2.12.0-beta.23"
    assert release_version(repo, preview=24) == "2.13.0-beta.24"


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        (["fix: Retry discovery", "Update README"], "2.12.1"),
        (["fix: Retry discovery", "feat(sensors): Add a sensor"], "2.13.0"),
        (["feat: Add a sensor", "fix(auth)!: Replace old credentials"], "3.0.0"),
        (["feat: Add a sensor\n\nBREAKING CHANGE: Requires migration"], "3.0.0"),
        (["Merge pull request #1 from test/branch\n\nfeat: Add a sensor"], "2.13.0"),
    ],
)
def test_highest_conventional_bump_since_stable(repo, messages, expected):
    git(repo, "tag", "2.12.0")
    for message in messages:
        git(repo, "commit", "--allow-empty", "-qm", message)

    assert release_version(repo) == expected


def test_production_rerun_and_superseded_commit(repo):
    previous = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "--allow-empty", "-qm", "feat: Home Assistant integration")
    git(repo, "tag", "2.12.0")
    assert release_version(repo) == "2.12.0"

    git(repo, "switch", "--detach", "-q", previous)
    assert release_version(repo) is None


@pytest.mark.parametrize(
    ("hacs", "prefix"), [(False, "custom_components/isponsorblocktv/"), (True, "")]
)
def test_release_zip_stamps_version_and_bundles_runtime(tmp_path, hacs, prefix):
    output = tmp_path / "test.zip"
    version = "2.12.0-beta.23" if hacs else "2.12.0"

    build(output, version=version, hacs=hacs)

    with ZipFile(output) as archive:
        assert archive.testzip() is None
        assert {
            f"{prefix}__init__.py",
            f"{prefix}core/playback.py",
            f"{prefix}translations/en.json",
        } <= set(archive.namelist())
        assert json.loads(archive.read(f"{prefix}manifest.json"))["version"] == version
        assert all(name.startswith(prefix) for name in archive.namelist())
        if hacs:
            assert not any(name.startswith("custom_components/") for name in archive.namelist())
