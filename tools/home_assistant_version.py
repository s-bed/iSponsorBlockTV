"""Choose the next Home Assistant release version from Git history."""

import re
import subprocess
import tomllib
from argparse import ArgumentParser
from pathlib import Path

STABLE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
CONVENTIONAL_SUBJECT = re.compile(r"([a-z][a-z0-9-]*)(?:\([^()]+\))?(!)?:\s")
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:\s", re.MULTILINE)


def git(repo: Path, *args: str) -> str:
    """Read release history from the checked-out repository."""
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _number(tag: str) -> tuple[int, int, int]:
    major, minor, patch = map(int, tag.split("."))
    return major, minor, patch


def _bump(version: str, messages: list[str]) -> str:
    major, minor, patch = _number(version)
    feature = False
    for message in messages:
        subject, _, body = message.strip().partition("\n")
        if subject.startswith("Merge pull request #"):
            subject = body.strip().partition("\n")[0]
        conventional = CONVENTIONAL_SUBJECT.match(subject)
        if BREAKING_FOOTER.search(body) or (conventional and conventional[2]):
            return f"{major + 1}.0.0"
        feature |= bool(conventional and conventional[1] == "feat")
    return f"{major}.{minor + 1}.0" if feature else f"{major}.{minor}.{patch + 1}"


def release_version(repo: Path, *, preview: int | None = None) -> str | None:
    """Return a tag for HEAD, or skip an older production run."""
    tags = git(repo, "tag", "--list").splitlines()
    stable = max((tag for tag in tags if STABLE.fullmatch(tag)), key=_number, default=None)
    head = git(repo, "rev-parse", "HEAD")
    if preview is not None:
        for tag in tags:
            if tag.endswith(f"-beta.{preview}") and STABLE.fullmatch(tag.split("-")[0]):
                if git(repo, "rev-parse", f"{tag}^{{commit}}") != head:
                    raise ValueError(f"Preview version {tag} already belongs to a different commit")
                return tag
    elif stable:
        if git(repo, "rev-parse", f"{stable}^{{commit}}") == head:
            return stable
        # A delayed run must never publish an older revision as the latest release.
        if git(repo, "merge-base", head, stable) == head:
            return None

    if stable:
        messages = git(repo, "log", "--format=%B%x00", f"{stable}..HEAD").split("\0")
        version = _bump(stable, messages)
    else:
        # Start above the standalone versions that HACS may see in the same repository.
        project = tomllib.loads(git(repo, "show", "HEAD:pyproject.toml"))["project"]["version"]
        standalone = [tag[1:] for tag in tags if tag.startswith("v") and STABLE.fullmatch(tag[1:])]
        version = _bump(
            max([project, *standalone], key=_number), ["feat: Home Assistant integration"]
        )

    return version if preview is None else f"{version}-beta.{preview}"


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=int, help="Development workflow run number")
    args = parser.parse_args()
    if args.preview is not None and args.preview <= 0:
        parser.error("--preview must be a positive integer")
    if version := release_version(Path.cwd(), preview=args.preview):
        print(version)
