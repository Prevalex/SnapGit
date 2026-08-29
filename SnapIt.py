#!/usr/bin/env python3
"""Create a timestamped backup of tracked, untracked, and ignored Git files."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_COPY_INCOMPLETE = 2


class SnapItError(RuntimeError):
    """A user-facing SnapIt error."""


@dataclass(frozen=True)
class IgnoreRule:
    include: bool
    regex: re.Pattern[str]


@dataclass(frozen=True)
class CopyResult:
    copied: int
    missing: int
    failed: int
    errors: tuple[str, ...]


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Back up tracked, untracked, and Git-ignored project files, "
            "filtered through .backupignore."
        )
    )
    parser.add_argument(
        "--project-root",
        "-ProjectRoot",
        required=True,
        type=Path,
        help="Git repository root to back up.",
    )
    parser.add_argument(
        "--backup-root",
        "-BackupRoot",
        required=True,
        type=Path,
        help="Destination root for timestamped backups.",
    )
    return parser.parse_args(argv)


def normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def paths_equal(first: Path, second: Path) -> bool:
    return normalized_path(first) == normalized_path(second)


def is_same_or_within(path: Path, parent: Path) -> bool:
    path_text = normalized_path(path)
    parent_text = normalized_path(parent)
    try:
        return os.path.commonpath((path_text, parent_text)) == parent_text
    except ValueError:
        # Different Windows drives cannot contain one another.
        return False


def resolve_project_root(project_root_argument: Path) -> Path:
    project_root = project_root_argument.expanduser().resolve(strict=False)
    if not project_root.is_dir():
        raise SnapItError(
            f"ProjectRoot does not exist or is not a directory: {project_root}"
        )

    requirements = (
        (".git", "directory", Path.is_dir),
        (".gitignore", "file (it may be empty)", Path.is_file),
        (".backupignore", "file (it may be empty)", Path.is_file),
    )
    missing: list[str] = []
    for name, expected_type, predicate in requirements:
        if not predicate(project_root / name):
            missing.append(f"{name} [{expected_type}]")

    if missing:
        raise SnapItError(
            "ProjectRoot is missing required items: "
            f"{', '.join(missing)}. Root: {project_root}"
        )

    git_root_output = run_git(project_root, "rev-parse", "--show-toplevel")
    try:
        git_root_text = git_root_output.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise SnapItError(f"Git returned a non-UTF-8 repository path: {error}") from error

    git_root = Path(git_root_text).resolve(strict=False)
    if not paths_equal(git_root, project_root):
        raise SnapItError(
            "ProjectRoot must point to the repository root. "
            f"Git reports: {git_root}"
        )

    return project_root


def resolve_backup_root(backup_root_argument: Path, project_root: Path) -> Path:
    backup_root = backup_root_argument.expanduser().resolve(strict=False)
    project_backup_root = backup_root / project_root.name

    if is_same_or_within(backup_root, project_root) or is_same_or_within(
        project_backup_root, project_root
    ):
        raise SnapItError(
            "BackupRoot must be outside the Git project to prevent recursive backups."
        )

    return backup_root


def run_git(working_directory: Path, *arguments: str) -> bytes:
    if shutil.which("git") is None:
        raise SnapItError("Git is unavailable or is not present in PATH.")

    command = ("git", "-c", "core.quotepath=false", *arguments)
    try:
        result = subprocess.run(
            command,
            cwd=working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise SnapItError(f"Git could not be started: {error}") from error

    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        if not message:
            message = f"Git exited with code {result.returncode}."
        raise SnapItError(message)

    return result.stdout


def get_git_null_list(project_root: Path, *arguments: str) -> list[str]:
    output = run_git(project_root, *arguments, "-z")
    if not output:
        return []

    paths: list[str] = []
    for item in output.split(b"\0"):
        if item:
            paths.append(item.decode("utf-8", errors="surrogateescape"))
    return paths


def pattern_to_regex(pattern: str) -> str:
    pattern_text = pattern.replace("\\", "/")
    root_anchored = pattern_text.startswith("/")
    if root_anchored:
        pattern_text = pattern_text[1:]

    directory_rule = pattern_text.endswith("/")
    if directory_rule:
        pattern_text = pattern_text.rstrip("/")

    contains_slash = "/" in pattern_text
    escaped = re.escape(pattern_text)
    escaped = escaped.replace(r"\*\*", ".*")
    escaped = escaped.replace(r"\*", "[^/]*")
    escaped = escaped.replace(r"\?", "[^/]")

    prefix = "^" if root_anchored or contains_slash else r"(^|.*/)"
    suffix = r"(/.*)?$" if directory_rule else "$"
    return prefix + escaped + suffix


def read_backup_ignore(path: Path) -> list[IgnoreRule]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as error:
        raise SnapItError(f"Could not read {path}: {error}") from error

    rules: list[IgnoreRule] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        include = line.startswith("!")
        if include:
            line = line[1:]
        if not line:
            continue

        try:
            compiled = re.compile(pattern_to_regex(line), re.IGNORECASE)
        except re.error as error:
            raise SnapItError(
                f"Invalid .backupignore pattern {raw_line!r}: {error}"
            ) from error
        rules.append(IgnoreRule(include=include, regex=compiled))

    return rules


def is_included(relative_path: str, rules: Iterable[IgnoreRule]) -> bool:
    path_text = relative_path.replace("\\", "/").lstrip("/")
    included = True
    for rule in rules:
        if rule.regex.search(path_text):
            included = rule.include
    return included


def unique_paths(*groups: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            normalized = path.replace("\\", "/")
            key = normalized.casefold()
            if key not in seen:
                seen.add(key)
                result.append(normalized)
    return result


def write_utf8_lines(path: Path, lines: Iterable[str]) -> None:
    line_list = list(lines)
    content = "\n".join(line_list)
    if line_list:
        content += "\n"
    try:
        path.write_text(content, encoding="utf-8", errors="surrogateescape")
    except (OSError, UnicodeError) as error:
        raise SnapItError(f"Could not write {path}: {error}") from error


def create_snapshot_root(backup_root: Path, project_name: str) -> Path:
    now = datetime.now()
    snapshot_root = (
        backup_root / project_name / now.strftime("%Y-%m-%d") / now.strftime("%H-%M-%S")
    )
    if snapshot_root.exists():
        raise SnapItError(
            f"Backup destination already exists: {snapshot_root}. "
            "Run the program again in a second."
        )

    try:
        (snapshot_root / "backup").mkdir(parents=True, exist_ok=False)
    except OSError as error:
        raise SnapItError(
            f"Could not create backup destination {snapshot_root}: {error}"
        ) from error
    return snapshot_root


def copy_files(
    project_root: Path, content_root: Path, relative_paths: Iterable[str]
) -> CopyResult:
    copied = 0
    missing = 0
    failed = 0
    errors: list[str] = []

    for relative_path in relative_paths:
        path_parts = relative_path.replace("\\", "/").split("/")
        source = project_root.joinpath(*path_parts)
        destination = content_root.joinpath(*path_parts)

        if not source.is_file():
            missing += 1
            errors.append(f"MISSING: {relative_path}")
            continue

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=True)
            copied += 1
        except OSError as error:
            failed += 1
            errors.append(f"FAILED: {relative_path} :: {error}")

    return CopyResult(
        copied=copied,
        missing=missing,
        failed=failed,
        errors=tuple(errors),
    )


def create_backup(project_root_argument: Path, backup_root_argument: Path) -> int:
    project_root = resolve_project_root(project_root_argument)
    backup_root = resolve_backup_root(backup_root_argument, project_root)
    project_name = project_root.name
    if not project_name:
        raise SnapItError(f"Could not determine the project name from {project_root}.")

    print(f"Project: {project_name}")
    print(f"Root:    {project_root}")
    print("Reading file lists from Git...")

    tracked = get_git_null_list(project_root, "ls-files")
    untracked = get_git_null_list(
        project_root, "ls-files", "--others", "--exclude-standard"
    )
    ignored = get_git_null_list(
        project_root,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
    )

    rules = read_backup_ignore(project_root / ".backupignore")
    candidates = unique_paths(tracked, untracked, ignored)
    backup_files = sorted(
        (path for path in candidates if is_included(path, rules)),
        key=str.casefold,
    )
    excluded_count = len(candidates) - len(backup_files)

    snapshot_root = create_snapshot_root(backup_root, project_name)
    content_root = snapshot_root / "backup"

    write_utf8_lines(snapshot_root / "tracked.txt", tracked)
    write_utf8_lines(snapshot_root / "untracked.txt", untracked)
    write_utf8_lines(snapshot_root / "ignored.txt", ignored)
    write_utf8_lines(snapshot_root / "backup-files.txt", backup_files)

    copy_result = copy_files(project_root, content_root, backup_files)
    if copy_result.errors:
        write_utf8_lines(snapshot_root / "copy-errors.txt", copy_result.errors)

    print()
    print("Backup complete.")
    print(f"Destination: {snapshot_root}")
    print(f"Tracked:    {len(tracked)}")
    print(f"Untracked:  {len(untracked)}")
    print(f"Ignored:    {len(ignored)}")
    print(f"Unique:     {len(candidates)}")
    print(f"Excluded:   {excluded_count}")
    print(f"Selected:   {len(backup_files)}")
    print(f"Copied:     {copy_result.copied}")
    print(f"Missing:    {copy_result.missing}")
    print(f"Failed:     {copy_result.failed}")

    if copy_result.missing or copy_result.failed:
        print(
            f"WARNING: Some files were not copied. See: "
            f"{snapshot_root / 'copy-errors.txt'}",
            file=sys.stderr,
        )
        return EXIT_COPY_INCOMPLETE
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    try:
        return create_backup(arguments.project_root, arguments.backup_root)
    except SnapItError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("ERROR: Interrupted by user.", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())

