#!/usr/bin/env python3
"""Create and restore timestamped snapshots of Git working trees."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_COPY_INCOMPLETE = 2
SNAPSHOT_FORMAT_VERSION = 1
PURGE_DIRECTORY_NAME = "$purge$"
DATE_DIRECTORY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
TIME_DIRECTORY_PATTERN = re.compile(r"\d{2}-\d{2}-\d{2}")


class SnapGitError(RuntimeError):
    """A user-facing SnapGit error."""


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


@dataclass(frozen=True)
class CompactionResult:
    compacted_days: int
    moved_snapshots: int


@dataclass(frozen=True)
class MonthlyCompactionResult:
    cutoff_month: str
    compacted_project_months: int
    moved_snapshots: int


@dataclass(frozen=True)
class RestoreItem:
    relative_path: str
    source: Path
    destination: Path


@dataclass(frozen=True)
class RestoreResult:
    candidates: int
    planned: int
    restored: int
    tracked_conflicts: int
    existing_conflicts: int
    missing: int
    failed: int
    errors: tuple[str, ...]


@dataclass(frozen=True)
class FullRestoreResult:
    head_commit: str
    planned_files: int
    restored_files: int
    planned_deletions: int
    deleted_files: int
    failed: int
    errors: tuple[str, ...]


def parse_month_offset(value: str) -> int:
    try:
        offset = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("month offset must be an integer") from error
    if offset > 0:
        raise argparse.ArgumentTypeError(
            "month offset must be zero or negative"
        )
    return offset


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    snap_root_from_env = os.environ.get("SNAP_ROOT")
    if snap_root_from_env is not None and not snap_root_from_env.strip():
        snap_root_from_env = None
    source_root_from_env = os.environ.get("SNAP_SOURCE_ROOT")
    if source_root_from_env is not None and not source_root_from_env.strip():
        source_root_from_env = None

    parser = argparse.ArgumentParser(
        description="Create and restore Git project snapshots."
    )
    command_parsers = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
    )

    backup_parser = command_parsers.add_parser(
        "backup",
        help="Create a timestamped project snapshot.",
        description=(
            "Back up tracked, untracked, and Git-ignored project files, "
            "filtered through .snapignore."
        ),
    )
    backup_parser.add_argument(
        "project_root",
        type=Path,
        metavar="PROJECT_ROOT",
        help="Git repository root to back up.",
    )
    backup_parser.add_argument(
        "--snap-root",
        "-sr",
        type=Path,
        default=snap_root_from_env,
        metavar="SNAP_ROOT",
        help=(
            "Destination root for timestamped snapshots. "
            "Defaults to the SNAP_ROOT environment variable."
        ),
    )
    backup_parser.add_argument(
        "--source-root",
        type=Path,
        default=source_root_from_env,
        metavar="SOURCE_ROOT",
        help=(
            "Optional ancestor directory whose relative project layout is "
            "recreated below SnapRoot. Defaults to SNAP_SOURCE_ROOT."
        ),
    )
    backup_parser.add_argument(
        "--compact-day",
        "-cd",
        action="store_true",
        help=(
            "After a successful backup, keep only the latest snapshot for each "
            "project and day, moving older snapshots below SnapRoot/$purge$."
        ),
    )
    backup_parser.add_argument(
        "--compact-month",
        "-cm",
        nargs="?",
        const=-6,
        type=parse_month_offset,
        default=None,
        metavar="MONTH_OFFSET",
        help=(
            "For every project and every month up to the selected cutoff month, "
            "keep only the latest snapshot. Use 0 for the current month, -1 for "
            "the previous month, and so on. When omitted, MONTH_OFFSET defaults "
            "to -6."
        ),
    )

    restore_parser = command_parsers.add_parser(
        "restore",
        help="Restore local extras or a complete saved working tree.",
        description=(
            "Restore local extra files safely or reconstruct the complete "
            "working tree recorded by a new-format snapshot."
        ),
    )
    restore_parser.add_argument(
        "project_root",
        type=Path,
        metavar="PROJECT_ROOT",
        help="Existing Git repository that receives restored files.",
    )
    restore_parser.add_argument(
        "--snapshot",
        required=True,
        type=Path,
        metavar="SNAPSHOT",
        help="Snapshot directory containing backup and the .lst files.",
    )
    restore_parser.add_argument(
        "--mode",
        choices=("extras", "full"),
        default="extras",
        help=(
            "Restore only snapshot-time untracked/ignored files (extras, "
            "default) or reconstruct the saved Git working tree (full)."
        ),
    )
    restore_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be restored without writing files.",
    )
    restore_parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Overwrite existing untracked or ignored destination files. "
            "Extras mode never overwrites Git-tracked files; full mode restores "
            "tracked files after checking out the saved commit."
        ),
    )
    restore_parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help=(
            "Allow restoration from a snapshot marked incomplete or containing "
            "copy-errors.lst."
        ),
    )

    arguments = parser.parse_args(raw_arguments)
    if arguments.command == "backup" and arguments.snap_root is None:
        backup_parser.error(
            "the following argument is required when SNAP_ROOT is not set: "
            "--snap-root/-sr"
        )
    return arguments


def normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def normalize_relative_path(path: str) -> str:
    if os.name == "nt":
        return path.replace("\\", "/")
    return path


def relative_path_key(path: str, *, ignore_case: bool) -> str:
    normalized = normalize_relative_path(path)
    return normalized.casefold() if ignore_case else normalized


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
        raise SnapGitError(
            f"ProjectRoot does not exist or is not a directory: {project_root}"
        )

    requirements = (
        (".git", "directory", Path.is_dir),
        (".gitignore", "file (it may be empty)", Path.is_file),
        (".snapignore", "file (it may be empty)", Path.is_file),
    )
    missing: list[str] = []
    for name, expected_type, predicate in requirements:
        if not predicate(project_root / name):
            missing.append(f"{name} [{expected_type}]")

    if missing:
        raise SnapGitError(
            "ProjectRoot is missing required items: "
            f"{', '.join(missing)}. Root: {project_root}"
        )

    git_root_output = run_git(project_root, "rev-parse", "--show-toplevel")
    try:
        git_root_text = git_root_output.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise SnapGitError(
            f"Git returned a non-UTF-8 repository path: {error}"
        ) from error

    git_root = Path(git_root_text).resolve(strict=False)
    if not paths_equal(git_root, project_root):
        raise SnapGitError(
            "ProjectRoot must point to the repository root. "
            f"Git reports: {git_root}"
        )

    return project_root


def resolve_restore_project_root(project_root_argument: Path) -> Path:
    project_root = project_root_argument.expanduser().resolve(strict=False)
    if not project_root.is_dir():
        raise SnapGitError(
            f"ProjectRoot does not exist or is not a directory: {project_root}"
        )
    if not (project_root / ".git").is_dir():
        raise SnapGitError(
            f"ProjectRoot does not contain a .git directory: {project_root}"
        )

    git_root_output = run_git(project_root, "rev-parse", "--show-toplevel")
    try:
        git_root_text = git_root_output.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise SnapGitError(
            f"Git returned a non-UTF-8 repository path: {error}"
        ) from error

    git_root = Path(git_root_text).resolve(strict=False)
    if not paths_equal(git_root, project_root):
        raise SnapGitError(
            "ProjectRoot must point to the repository root. "
            f"Git reports: {git_root}"
        )
    return project_root


def resolve_source_root(
    source_root_argument: Path | None, project_root: Path
) -> Path | None:
    if source_root_argument is None:
        return None

    source_root = source_root_argument.expanduser().resolve(strict=False)
    if not source_root.is_dir():
        raise SnapGitError(
            f"SourceRoot does not exist or is not a directory: {source_root}"
        )
    if paths_equal(source_root, project_root) or not is_same_or_within(
        project_root, source_root
    ):
        raise SnapGitError(
            "ProjectRoot must be inside SourceRoot. "
            f"ProjectRoot: {project_root}. SourceRoot: {source_root}"
        )
    return source_root


def get_project_snapshot_path(project_root: Path, source_root: Path | None) -> Path:
    if source_root is None:
        return Path(project_root.name)
    return Path(os.path.relpath(project_root, source_root))


def resolve_snap_root(
    snap_root_argument: Path,
    project_root: Path,
    project_snapshot_path: Path,
) -> Path:
    snap_root = snap_root_argument.expanduser().resolve(strict=False)
    project_snap_root = snap_root / project_snapshot_path

    if is_same_or_within(snap_root, project_root) or is_same_or_within(
        project_snap_root, project_root
    ):
        raise SnapGitError(
            "SnapRoot must be outside the Git project to prevent recursive snapshots."
        )

    return snap_root


def run_git(working_directory: Path, *arguments: str) -> bytes:
    if shutil.which("git") is None:
        raise SnapGitError("Git is unavailable or is not present in PATH.")

    command = ("git", "-c", "core.quotepath=false", *arguments)
    try:
        result = subprocess.run(
            command,
            cwd=working_directory,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise SnapGitError(f"Git could not be started: {error}") from error

    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        if not message:
            message = f"Git exited with code {result.returncode}."
        raise SnapGitError(message)

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


def get_optional_git_text(project_root: Path, *arguments: str) -> str | None:
    command = ("git", "-c", "core.quotepath=false", *arguments)
    try:
        result = subprocess.run(
            command,
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise SnapGitError(f"Git could not be started: {error}") from error
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace").strip() or None


def sanitize_remote_url(remote_url: str | None) -> str | None:
    if remote_url is None:
        return None
    return re.sub(r"(?i)(https?://)[^/@]+@", r"\1", remote_url)


def get_git_ignore_case(project_root: Path) -> bool:
    configured = get_optional_git_text(
        project_root,
        "config",
        "--bool",
        "core.ignorecase",
    )
    if configured in {"true", "false"}:
        return configured == "true"
    return os.path.normcase("SnapGit") == os.path.normcase("snapgit")


def get_git_snapshot_metadata(
    project_root: Path, *, ignore_case: bool
) -> dict[str, object]:
    staged = get_git_null_list(project_root, "diff", "--cached", "--name-only")
    unstaged = get_git_null_list(project_root, "diff", "--name-only")
    staged_deleted = get_git_null_list(
        project_root,
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=D",
    )
    unstaged_deleted = get_git_null_list(
        project_root,
        "diff",
        "--name-only",
        "--diff-filter=D",
    )
    deletion_candidates = unique_paths(
        staged_deleted,
        unstaged_deleted,
        ignore_case=ignore_case,
    )
    deleted: list[str] = []
    for relative_path in deletion_candidates:
        _, path_parts = validate_restore_relative_path(relative_path)
        if not os.path.lexists(project_root.joinpath(*path_parts)):
            deleted.append(relative_path)
    return {
        "head": get_optional_git_text(project_root, "rev-parse", "--verify", "HEAD"),
        "branch": get_optional_git_text(
            project_root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
        ),
        "remote_origin": sanitize_remote_url(
            get_optional_git_text(project_root, "config", "--get", "remote.origin.url")
        ),
        "staged": staged,
        "unstaged": unstaged,
        "modified": unique_paths(staged, unstaged, ignore_case=ignore_case),
        "deleted": deleted,
        "ignore_case": ignore_case,
    }


def read_utf8_line_list(path: Path) -> list[str]:
    try:
        return path.read_text(
            encoding="utf-8-sig",
            errors="surrogateescape",
        ).splitlines()
    except (OSError, UnicodeError) as error:
        raise SnapGitError(f"Could not read {path}: {error}") from error


def read_snapshot_manifest(snapshot_root: Path) -> dict[str, object]:
    manifest_path = snapshot_root / "snapshot.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SnapGitError(
            "Full restore requires snapshot.json from the new snapshot format."
        ) from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SnapGitError(f"Could not read {manifest_path}: {error}") from error
    if not isinstance(manifest, dict):
        raise SnapGitError(f"Invalid snapshot manifest: {manifest_path}")
    if manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise SnapGitError(
            "Unsupported snapshot format version: "
            f"{manifest.get('format_version')!r}"
        )
    return manifest


def write_snapshot_manifest(path: Path, manifest: dict[str, object]) -> None:
    try:
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            errors="backslashreplace",
        )
    except (OSError, UnicodeError, TypeError) as error:
        raise SnapGitError(f"Could not write {path}: {error}") from error


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_restore_relative_path(relative_path: str) -> tuple[str, tuple[str, ...]]:
    normalized = normalize_relative_path(relative_path)
    parts = tuple(normalized.split("/"))
    if (
        not normalized
        or normalized.startswith("/")
        or (os.name == "nt" and re.match(r"^[A-Za-z]:", normalized) is not None)
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise SnapGitError(
            f"Snapshot contains an unsafe relative path: {relative_path!r}"
        )
    return normalized, parts


def pattern_to_regex(pattern: str) -> str:
    pattern_text = normalize_relative_path(pattern)
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


def read_snap_ignore(path: Path, *, ignore_case: bool) -> list[IgnoreRule]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as error:
        raise SnapGitError(f"Could not read {path}: {error}") from error

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
            flags = re.IGNORECASE if ignore_case else 0
            compiled = re.compile(pattern_to_regex(line), flags)
        except re.error as error:
            raise SnapGitError(
                f"Invalid .snapignore pattern {raw_line!r}: {error}"
            ) from error
        rules.append(IgnoreRule(include=include, regex=compiled))

    return rules


def is_included(relative_path: str, rules: Iterable[IgnoreRule]) -> bool:
    path_text = normalize_relative_path(relative_path).lstrip("/")
    included = True
    for rule in rules:
        if rule.regex.search(path_text):
            included = rule.include
    return included


def unique_paths(
    *groups: Iterable[str], ignore_case: bool
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for path in group:
            normalized = normalize_relative_path(path)
            key = relative_path_key(normalized, ignore_case=ignore_case)
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
        raise SnapGitError(f"Could not write {path}: {error}") from error


def create_snapshot_root(snap_root: Path, project_snapshot_path: Path) -> Path:
    now = datetime.now()
    snapshot_root = (
        snap_root
        / project_snapshot_path
        / now.strftime("%Y-%m-%d")
        / now.strftime("%H-%M-%S")
    )
    if snapshot_root.exists():
        raise SnapGitError(
            f"Backup destination already exists: {snapshot_root}. "
            "Run the program again in a second."
        )

    try:
        (snapshot_root / "backup").mkdir(parents=True, exist_ok=False)
    except OSError as error:
        raise SnapGitError(
            f"Could not create backup destination {snapshot_root}: {error}"
        ) from error
    return snapshot_root


def is_snapshot_directory(path: Path) -> bool:
    return (
        path.is_dir()
        and TIME_DIRECTORY_PATTERN.fullmatch(path.name) is not None
        and (path / "backup").is_dir()
    )


def find_snapshot_days(snap_root: Path) -> Iterable[tuple[Path, list[Path]]]:
    for current_text, directory_names, _ in os.walk(snap_root):
        current = Path(current_text)
        if paths_equal(current, snap_root):
            directory_names[:] = [
                name
                for name in directory_names
                if os.path.normcase(name) != os.path.normcase(PURGE_DIRECTORY_NAME)
            ]

        if DATE_DIRECTORY_PATTERN.fullmatch(current.name) is None:
            continue

        snapshots = [
            current / name
            for name in directory_names
            if is_snapshot_directory(current / name)
        ]
        snapshot_names = {path.name for path in snapshots}
        directory_names[:] = [
            name for name in directory_names if name not in snapshot_names
        ]
        if snapshots:
            yield current, snapshots


def move_snapshots_to_purge(
    snap_root: Path, snapshots: Iterable[Path]
) -> int:
    purge_root = snap_root / PURGE_DIRECTORY_NAME
    moves: list[tuple[Path, Path]] = []
    for snapshot in snapshots:
        relative_path = snapshot.relative_to(snap_root)
        destination = purge_root / relative_path
        if destination.exists():
            raise SnapGitError(
                "Compaction would overwrite an existing purge snapshot: "
                f"{destination}"
            )
        moves.append((snapshot, destination))

    for snapshot, destination in moves:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            snapshot.rename(destination)
        except OSError as error:
            raise SnapGitError(
                f"Could not move snapshot {snapshot} to {destination}: {error}"
            ) from error

    return len(moves)


def compact_daily_snapshots(snap_root_argument: Path) -> CompactionResult:
    snap_root = snap_root_argument.expanduser().resolve(strict=False)
    if not snap_root.is_dir():
        raise SnapGitError(
            f"SnapRoot does not exist or is not a directory: {snap_root}"
        )

    snapshots_to_move: list[Path] = []
    compacted_days = 0

    for _, snapshots in find_snapshot_days(snap_root):
        ordered = sorted(snapshots, key=lambda path: path.name)
        if len(ordered) <= 1:
            continue

        compacted_days += 1
        snapshots_to_move.extend(ordered[:-1])

    moved_snapshots = move_snapshots_to_purge(snap_root, snapshots_to_move)

    return CompactionResult(
        compacted_days=compacted_days,
        moved_snapshots=moved_snapshots,
    )


def get_cutoff_month(month_offset: int, now: datetime | None = None) -> str:
    if month_offset > 0:
        raise SnapGitError("Month offset must be zero or negative.")

    reference = now if now is not None else datetime.now()
    month_index = reference.year * 12 + reference.month - 1 + month_offset
    year, zero_based_month = divmod(month_index, 12)
    if year < 1:
        raise SnapGitError(f"Month offset is too small: {month_offset}")
    return f"{year:04d}-{zero_based_month + 1:02d}"


def compact_monthly_snapshots(
    snap_root_argument: Path,
    month_offset: int,
    now: datetime | None = None,
) -> MonthlyCompactionResult:
    snap_root = snap_root_argument.expanduser().resolve(strict=False)
    if not snap_root.is_dir():
        raise SnapGitError(
            f"SnapRoot does not exist or is not a directory: {snap_root}"
        )

    cutoff_month = get_cutoff_month(month_offset, now)
    monthly_snapshots: dict[tuple[Path, str], list[Path]] = {}
    for date_path, snapshots in find_snapshot_days(snap_root):
        snapshot_month = date_path.name[:7]
        if snapshot_month > cutoff_month:
            continue
        group_key = (date_path.parent, snapshot_month)
        monthly_snapshots.setdefault(group_key, []).extend(snapshots)

    snapshots_to_move: list[Path] = []
    compacted_project_months = 0
    for snapshots in monthly_snapshots.values():
        ordered = sorted(
            snapshots,
            key=lambda path: (path.parent.name, path.name),
        )
        if len(ordered) <= 1:
            continue
        compacted_project_months += 1
        snapshots_to_move.extend(ordered[:-1])

    moved_snapshots = move_snapshots_to_purge(snap_root, snapshots_to_move)
    return MonthlyCompactionResult(
        cutoff_month=cutoff_month,
        compacted_project_months=compacted_project_months,
        moved_snapshots=moved_snapshots,
    )


def copy_files(
    project_root: Path, content_root: Path, relative_paths: Iterable[str]
) -> CopyResult:
    copied = 0
    missing = 0
    failed = 0
    errors: list[str] = []

    for relative_path in relative_paths:
        path_parts = normalize_relative_path(relative_path).split("/")
        source = project_root.joinpath(*path_parts)
        destination = content_root.joinpath(*path_parts)

        try:
            source_is_symlink = source.is_symlink()
            source_is_file = source.is_file()
        except OSError as error:
            failed += 1
            errors.append(f"FAILED: {relative_path} :: {error}")
            continue

        if not source_is_file and not source_is_symlink:
            missing += 1
            errors.append(f"MISSING: {relative_path}")
            continue

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source_is_symlink:
                link_target = os.readlink(source)
                os.symlink(
                    link_target,
                    destination,
                    target_is_directory=source.is_dir(),
                )
            else:
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


def build_snapshot_file_entries(
    project_root: Path,
    content_root: Path,
    backup_files: Iterable[str],
    tracked: Iterable[str],
    untracked: Iterable[str],
    ignored: Iterable[str],
    *,
    ignore_case: bool,
) -> tuple[list[dict[str, object]], list[str]]:
    tracked_keys = {
        relative_path_key(path, ignore_case=ignore_case) for path in tracked
    }
    untracked_keys = {
        relative_path_key(path, ignore_case=ignore_case) for path in untracked
    }
    ignored_keys = {
        relative_path_key(path, ignore_case=ignore_case) for path in ignored
    }
    entries: list[dict[str, object]] = []
    errors: list[str] = []

    for raw_path in backup_files:
        relative_path, path_parts = validate_restore_relative_path(raw_path)
        path_key = relative_path_key(relative_path, ignore_case=ignore_case)
        if path_key in tracked_keys:
            category = "tracked"
        elif path_key in untracked_keys:
            category = "untracked"
        elif path_key in ignored_keys:
            category = "ignored"
        else:
            category = "unknown"

        source = project_root.joinpath(*path_parts)
        archived = content_root.joinpath(*path_parts)
        entry: dict[str, object] = {
            "path": relative_path,
            "category": category,
            "copied": False,
            "size": None,
            "sha256": None,
            "mode": None,
            "symlink_target": None,
            "symlink_is_directory": None,
        }

        try:
            archived_is_symlink = archived.is_symlink()
            archived_is_file = archived.is_file()
            if not archived_is_symlink and not archived_is_file:
                entries.append(entry)
                continue

            entry["copied"] = True
            source_status = source.lstat()
            entry["mode"] = stat.S_IMODE(source_status.st_mode)
            if source.is_symlink():
                entry["symlink_target"] = os.readlink(source)
                entry["symlink_is_directory"] = source.is_dir()
            else:
                archived_status = archived.stat()
                entry["size"] = archived_status.st_size
                entry["sha256"] = sha256_file(archived)
        except (OSError, UnicodeError) as error:
            entry["copied"] = False
            errors.append(f"FAILED-METADATA: {relative_path} :: {error}")
        entries.append(entry)

    return entries, errors


def restore_extra_files(
    project_root_argument: Path,
    snapshot_argument: Path,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    allow_incomplete: bool = False,
) -> RestoreResult:
    project_root = resolve_restore_project_root(project_root_argument)
    ignore_case = get_git_ignore_case(project_root)
    snapshot_root = snapshot_argument.expanduser().resolve(strict=False)
    if not snapshot_root.is_dir():
        raise SnapGitError(
            f"Snapshot does not exist or is not a directory: {snapshot_root}"
        )
    if is_same_or_within(snapshot_root, project_root):
        raise SnapGitError("Snapshot must be outside the restore target project.")

    required_items = (
        "tracked.lst",
        "untracked.lst",
        "ignored.lst",
        "backup-files.lst",
    )
    missing_items = [
        name for name in required_items if not (snapshot_root / name).is_file()
    ]
    backup_root = snapshot_root / "backup"
    if not backup_root.is_dir():
        missing_items.append("backup [directory]")
    if missing_items:
        raise SnapGitError(
            "Snapshot is missing required items: " + ", ".join(missing_items)
        )

    copy_errors_path = snapshot_root / "copy-errors.lst"
    if copy_errors_path.exists() and not allow_incomplete:
        raise SnapGitError(
            "Snapshot is incomplete because copy-errors.lst exists. "
            "Use --allow-incomplete to restore the files that are available."
        )
    if copy_errors_path.exists():
        print(
            f"WARNING: Restoring from an incomplete snapshot: {copy_errors_path}",
            file=sys.stderr,
        )

    untracked = read_utf8_line_list(snapshot_root / "untracked.lst")
    ignored = read_utf8_line_list(snapshot_root / "ignored.lst")
    backup_files = read_utf8_line_list(snapshot_root / "backup-files.lst")
    backup_keys = {
        relative_path_key(path, ignore_case=ignore_case) for path in backup_files
    }
    candidates = [
        path
        for path in unique_paths(untracked, ignored, ignore_case=ignore_case)
        if relative_path_key(path, ignore_case=ignore_case) in backup_keys
    ]

    validated_candidates: list[tuple[str, tuple[str, ...]]] = []
    for relative_path in candidates:
        validated_candidates.append(validate_restore_relative_path(relative_path))

    tracked_keys = {
        relative_path_key(path, ignore_case=ignore_case)
        for path in get_git_null_list(project_root, "ls-files")
    }
    planned_items: list[RestoreItem] = []
    errors: list[str] = []
    tracked_conflicts = 0
    existing_conflicts = 0
    missing = 0
    failed = 0

    for relative_path, path_parts in validated_candidates:
        source = backup_root.joinpath(*path_parts)
        destination = project_root.joinpath(*path_parts)
        path_key = relative_path_key(relative_path, ignore_case=ignore_case)

        if path_key in tracked_keys:
            tracked_conflicts += 1
            errors.append(f"TRACKED: {relative_path}")
            continue

        try:
            source_is_file = source.is_file()
        except OSError as error:
            failed += 1
            errors.append(f"FAILED: {relative_path} :: {error}")
            continue
        if not source_is_file:
            missing += 1
            errors.append(f"MISSING: {relative_path}")
            continue

        try:
            resolved_source = source.resolve(strict=True)
            resolved_destination = destination.resolve(strict=False)
        except OSError as error:
            failed += 1
            errors.append(f"FAILED: {relative_path} :: {error}")
            continue
        if not is_same_or_within(resolved_source, backup_root):
            raise SnapGitError(
                f"Snapshot file resolves outside its backup directory: {relative_path}"
            )
        if not is_same_or_within(resolved_destination, project_root):
            raise SnapGitError(
                f"Restore destination resolves outside ProjectRoot: {relative_path}"
            )

        try:
            destination_exists = os.path.lexists(destination)
            destination_is_directory = destination.is_dir()
            destination_is_symlink = destination.is_symlink()
        except OSError as error:
            failed += 1
            errors.append(f"FAILED: {relative_path} :: {error}")
            continue

        if destination_exists and (
            not overwrite or destination_is_directory or destination_is_symlink
        ):
            existing_conflicts += 1
            errors.append(f"EXISTS: {relative_path}")
            continue

        planned_items.append(
            RestoreItem(
                relative_path=relative_path,
                source=source,
                destination=destination,
            )
        )

    restored = 0
    if not dry_run:
        for item in planned_items:
            try:
                item.destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item.source, item.destination, follow_symlinks=True)
                restored += 1
            except OSError as error:
                failed += 1
                errors.append(f"FAILED: {item.relative_path} :: {error}")

    return RestoreResult(
        candidates=len(candidates),
        planned=len(planned_items),
        restored=restored,
        tracked_conflicts=tracked_conflicts,
        existing_conflicts=existing_conflicts,
        missing=missing,
        failed=failed,
        errors=tuple(errors),
    )


def restore_full_snapshot(
    project_root_argument: Path,
    snapshot_argument: Path,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    allow_incomplete: bool = False,
) -> FullRestoreResult:
    project_root = resolve_restore_project_root(project_root_argument)
    ignore_case = get_git_ignore_case(project_root)
    snapshot_root = snapshot_argument.expanduser().resolve(strict=False)
    if not snapshot_root.is_dir():
        raise SnapGitError(
            f"Snapshot does not exist or is not a directory: {snapshot_root}"
        )
    if is_same_or_within(snapshot_root, project_root):
        raise SnapGitError("Snapshot must be outside the restore target project.")

    manifest = read_snapshot_manifest(snapshot_root)
    manifest_status = manifest.get("status")
    if manifest_status != "complete" and not allow_incomplete:
        raise SnapGitError(
            f"Snapshot status is {manifest_status!r}. "
            "Use --allow-incomplete to restore the files that are available."
        )
    if manifest_status != "complete":
        print(
            f"WARNING: Restoring a snapshot with status {manifest_status!r}.",
            file=sys.stderr,
        )

    git_metadata = manifest.get("git")
    file_entries = manifest.get("files")
    if not isinstance(git_metadata, dict) or not isinstance(file_entries, list):
        raise SnapGitError("snapshot.json is missing valid git or files data.")
    head_commit = git_metadata.get("head")
    if not isinstance(head_commit, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40,64}", head_commit
    ):
        raise SnapGitError(
            "Full restore requires a valid saved Git HEAD commit."
        )

    try:
        run_git(project_root, "cat-file", "-e", f"{head_commit}^{{commit}}")
    except SnapGitError as error:
        raise SnapGitError(
            f"Saved Git commit is unavailable: {head_commit}. "
            "Fetch it from the repository remote before restoring."
        ) from error

    worktree_status = get_git_null_list(
        project_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if worktree_status:
        raise SnapGitError(
            "Full restore requires a clean Git working tree without untracked files."
        )

    saved_remote = git_metadata.get("remote_origin")
    current_remote = sanitize_remote_url(
        get_optional_git_text(project_root, "config", "--get", "remote.origin.url")
    )
    if (
        isinstance(saved_remote, str)
        and current_remote is not None
        and saved_remote != current_remote
    ):
        print(
            "WARNING: Snapshot and target remote.origin.url values differ. "
            "The saved commit is present, so restore can continue.",
            file=sys.stderr,
        )

    backup_root = snapshot_root / "backup"
    if not backup_root.is_dir():
        raise SnapGitError(f"Snapshot is missing its backup directory: {backup_root}")

    planned_items: list[tuple[RestoreItem, dict[str, object]]] = []
    extra_conflicts: list[str] = []
    manifest_paths: set[str] = set()
    for raw_entry in file_entries:
        if not isinstance(raw_entry, dict):
            raise SnapGitError("snapshot.json contains an invalid file entry.")
        if raw_entry.get("copied") is not True:
            continue
        raw_path = raw_entry.get("path")
        category = raw_entry.get("category")
        if (
            not isinstance(raw_path, str)
            or category not in {"tracked", "untracked", "ignored", "unknown"}
        ):
            raise SnapGitError(
                "snapshot.json contains an invalid file path or category."
            )
        relative_path, path_parts = validate_restore_relative_path(raw_path)
        path_key = relative_path_key(relative_path, ignore_case=ignore_case)
        if path_key in manifest_paths:
            raise SnapGitError(
                f"snapshot.json contains a duplicate file path: {relative_path}"
            )
        manifest_paths.add(path_key)
        source = backup_root.joinpath(*path_parts)
        destination = project_root.joinpath(*path_parts)

        try:
            resolved_destination = destination.resolve(strict=False)
        except OSError as error:
            raise SnapGitError(
                f"Could not resolve restore destination {destination}: {error}"
            ) from error
        if not is_same_or_within(resolved_destination, project_root):
            raise SnapGitError(
                f"Restore destination resolves outside ProjectRoot: {relative_path}"
            )

        symlink_target = raw_entry.get("symlink_target")
        if symlink_target is not None:
            try:
                source_is_symlink = source.is_symlink()
                archived_link_target = (
                    os.readlink(source) if source_is_symlink else None
                )
            except OSError as error:
                raise SnapGitError(
                    f"Could not verify snapshot symlink {relative_path}: {error}"
                ) from error
            if (
                not isinstance(symlink_target, str)
                or not source_is_symlink
                or archived_link_target != symlink_target
            ):
                raise SnapGitError(
                    f"Snapshot symlink data is invalid or missing: {relative_path}"
                )
        else:
            try:
                if not source.is_file():
                    raise SnapGitError(f"Snapshot file is missing: {relative_path}")
                resolved_source = source.resolve(strict=True)
                if not is_same_or_within(resolved_source, backup_root):
                    raise SnapGitError(
                        "Snapshot file resolves outside its backup directory: "
                        f"{relative_path}"
                    )
                expected_size = raw_entry.get("size")
                expected_hash = raw_entry.get("sha256")
                if (
                    not isinstance(expected_size, int)
                    or expected_size < 0
                    or not isinstance(expected_hash, str)
                    or re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) is None
                ):
                    raise SnapGitError(
                        f"Snapshot integrity data is missing: {relative_path}"
                    )
                if source.stat().st_size != expected_size:
                    raise SnapGitError(
                        f"Snapshot file size does not match manifest: {relative_path}"
                    )
                if sha256_file(source) != expected_hash:
                    raise SnapGitError(
                        f"Snapshot SHA-256 does not match manifest: {relative_path}"
                    )
            except OSError as error:
                raise SnapGitError(
                    f"Could not verify snapshot file {relative_path}: {error}"
                ) from error

        try:
            destination_exists = os.path.lexists(destination)
            destination_is_directory = destination.is_dir()
            destination_is_symlink = destination.is_symlink()
        except OSError as error:
            raise SnapGitError(
                f"Could not inspect restore destination {destination}: {error}"
            ) from error
        if destination_is_directory and not destination_is_symlink:
            extra_conflicts.append(relative_path)
            continue
        if category != "tracked" and destination_exists and not overwrite:
            extra_conflicts.append(relative_path)
            continue

        planned_items.append(
            (
                RestoreItem(
                    relative_path=relative_path,
                    source=source,
                    destination=destination,
                ),
                raw_entry,
            )
        )

    if extra_conflicts:
        preview = ", ".join(extra_conflicts[:5])
        if len(extra_conflicts) > 5:
            preview += ", ..."
        raise SnapGitError(
            "Full restore found existing local path conflicts. "
            f"Use --overwrite to replace files: {preview}"
        )

    deleted_paths = git_metadata.get("deleted", [])
    if not isinstance(deleted_paths, list) or not all(
        isinstance(path, str) for path in deleted_paths
    ):
        raise SnapGitError("snapshot.json contains an invalid Git deleted list.")
    validated_deletions = [
        validate_restore_relative_path(path) for path in deleted_paths
    ]
    duplicate_deletions: set[str] = set()
    for relative_path, _ in validated_deletions:
        path_key = relative_path_key(relative_path, ignore_case=ignore_case)
        if path_key in duplicate_deletions:
            raise SnapGitError(
                f"snapshot.json contains a duplicate deleted path: {relative_path}"
            )
        if path_key in manifest_paths:
            raise SnapGitError(
                "snapshot.json marks the same path as copied and deleted: "
                f"{relative_path}"
            )
        duplicate_deletions.add(path_key)

    if dry_run:
        return FullRestoreResult(
            head_commit=head_commit,
            planned_files=len(planned_items),
            restored_files=0,
            planned_deletions=len(validated_deletions),
            deleted_files=0,
            failed=0,
            errors=(),
        )

    run_git(project_root, "checkout", "--detach", head_commit)

    restored_files = 0
    deleted_files = 0
    failed = 0
    errors: list[str] = []
    for item, entry in planned_items:
        try:
            item.destination.parent.mkdir(parents=True, exist_ok=True)
            symlink_target = entry.get("symlink_target")
            if symlink_target is not None:
                if os.path.lexists(item.destination):
                    item.destination.unlink()
                os.symlink(
                    symlink_target,
                    item.destination,
                    target_is_directory=bool(entry.get("symlink_is_directory")),
                )
            else:
                if item.destination.is_symlink():
                    item.destination.unlink()
                shutil.copy2(item.source, item.destination, follow_symlinks=True)
                saved_mode = entry.get("mode")
                if isinstance(saved_mode, int) and 0 <= saved_mode <= 0o7777:
                    os.chmod(item.destination, saved_mode)
            restored_files += 1
        except OSError as error:
            failed += 1
            errors.append(f"FAILED: {item.relative_path} :: {error}")

    for relative_path, path_parts in validated_deletions:
        destination = project_root.joinpath(*path_parts)
        try:
            resolved_destination = destination.resolve(strict=False)
            if not is_same_or_within(resolved_destination, project_root):
                raise SnapGitError(
                    f"Delete destination resolves outside ProjectRoot: {relative_path}"
                )
            if os.path.lexists(destination):
                if destination.is_dir() and not destination.is_symlink():
                    raise OSError("tracked deletion path is unexpectedly a directory")
                destination.unlink()
                deleted_files += 1
        except OSError as error:
            failed += 1
            errors.append(f"FAILED-DELETE: {relative_path} :: {error}")

    return FullRestoreResult(
        head_commit=head_commit,
        planned_files=len(planned_items),
        restored_files=restored_files,
        planned_deletions=len(validated_deletions),
        deleted_files=deleted_files,
        failed=failed,
        errors=tuple(errors),
    )


def create_backup(
    project_root_argument: Path,
    snap_root_argument: Path,
    source_root_argument: Path | None = None,
) -> int:
    project_root = resolve_project_root(project_root_argument)
    source_root = resolve_source_root(source_root_argument, project_root)
    project_snapshot_path = get_project_snapshot_path(project_root, source_root)
    snap_root = resolve_snap_root(
        snap_root_argument,
        project_root,
        project_snapshot_path,
    )
    project_name = project_root.name
    if not project_name:
        raise SnapGitError(f"Could not determine the project name from {project_root}.")

    print(f"Project: {project_name}")
    print(f"Root:    {project_root}")
    if source_root is not None:
        print(f"Layout:  {project_snapshot_path}")
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
    ignore_case = get_git_ignore_case(project_root)
    git_metadata = get_git_snapshot_metadata(
        project_root,
        ignore_case=ignore_case,
    )
    deleted_keys = {
        relative_path_key(path, ignore_case=ignore_case)
        for path in git_metadata["deleted"]
    }

    rules = read_snap_ignore(
        project_root / ".snapignore",
        ignore_case=ignore_case,
    )
    candidates = unique_paths(
        tracked,
        untracked,
        ignored,
        ignore_case=ignore_case,
    )
    backup_files = sorted(
        (
            path
            for path in candidates
            if is_included(path, rules)
            and relative_path_key(path, ignore_case=ignore_case) not in deleted_keys
        ),
        key=lambda path: relative_path_key(path, ignore_case=ignore_case),
    )
    excluded_count = len(candidates) - len(backup_files)

    snapshot_root = create_snapshot_root(snap_root, project_snapshot_path)
    content_root = snapshot_root / "backup"

    write_utf8_lines(snapshot_root / "tracked.lst", tracked)
    write_utf8_lines(snapshot_root / "untracked.lst", untracked)
    write_utf8_lines(snapshot_root / "ignored.lst", ignored)
    write_utf8_lines(snapshot_root / "backup-files.lst", backup_files)

    copy_result = copy_files(project_root, content_root, backup_files)
    file_entries, metadata_errors = build_snapshot_file_entries(
        project_root,
        content_root,
        backup_files,
        tracked,
        untracked,
        ignored,
        ignore_case=ignore_case,
    )
    all_errors = (*copy_result.errors, *metadata_errors)
    if all_errors:
        write_utf8_lines(snapshot_root / "copy-errors.lst", all_errors)

    manifest: dict[str, object] = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "created_at": datetime.now().astimezone().isoformat(),
        "status": "incomplete" if all_errors else "complete",
        "project": {
            "name": project_name,
            "relative_path": project_snapshot_path.as_posix(),
            "source_root": str(source_root) if source_root is not None else None,
            "original_root": str(project_root),
        },
        "git": git_metadata,
        "files": file_entries,
        "copy_errors": list(all_errors),
    }
    write_snapshot_manifest(snapshot_root / "snapshot.json", manifest)

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
    print(f"Failed:     {copy_result.failed + len(metadata_errors)}")

    if copy_result.missing or copy_result.failed or metadata_errors:
        print(
            f"WARNING: Some files were not copied. See: "
            f"{snapshot_root / 'copy-errors.lst'}",
            file=sys.stderr,
        )
        return EXIT_COPY_INCOMPLETE
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    try:
        if arguments.command == "restore":
            if arguments.mode == "full":
                full_result = restore_full_snapshot(
                    arguments.project_root,
                    arguments.snapshot,
                    dry_run=arguments.dry_run,
                    overwrite=arguments.overwrite,
                    allow_incomplete=arguments.allow_incomplete,
                )
                print(
                    "Full restore dry run complete."
                    if arguments.dry_run
                    else "Full restore complete."
                )
                print(f"Saved HEAD:        {full_result.head_commit}")
                print(f"Planned files:     {full_result.planned_files}")
                print(f"Restored files:    {full_result.restored_files}")
                print(f"Planned deletions: {full_result.planned_deletions}")
                print(f"Deleted files:     {full_result.deleted_files}")
                print(f"Failed:            {full_result.failed}")
                if not arguments.dry_run:
                    print(
                        "NOTE: Saved staged/unstaged boundaries are recorded in "
                        "snapshot.json but are not reapplied to the Git index."
                    )
                for error_message in full_result.errors:
                    print(error_message, file=sys.stderr)
                if full_result.failed:
                    print(
                        "WARNING: Full restore completed with file errors.",
                        file=sys.stderr,
                    )
                    return EXIT_COPY_INCOMPLETE
                return EXIT_OK

            result = restore_extra_files(
                arguments.project_root,
                arguments.snapshot,
                dry_run=arguments.dry_run,
                overwrite=arguments.overwrite,
                allow_incomplete=arguments.allow_incomplete,
            )
            print(
                "Restore dry run complete."
                if arguments.dry_run
                else "Restore complete."
            )
            restore_target = arguments.project_root.expanduser().resolve(strict=False)
            restore_snapshot = arguments.snapshot.expanduser().resolve(strict=False)
            print(f"Target:             {restore_target}")
            print(f"Snapshot:           {restore_snapshot}")
            print(f"Candidates:         {result.candidates}")
            print(f"Planned:            {result.planned}")
            if arguments.dry_run:
                print(f"Would restore:      {result.planned}")
            else:
                print(f"Restored:           {result.restored}")
            print(f"Tracked conflicts:  {result.tracked_conflicts}")
            print(f"Existing conflicts: {result.existing_conflicts}")
            print(f"Missing:            {result.missing}")
            print(f"Failed:             {result.failed}")
            for error_message in result.errors:
                print(error_message, file=sys.stderr)

            if (
                result.tracked_conflicts
                or result.existing_conflicts
                or result.missing
                or result.failed
            ):
                print(
                    "WARNING: Some snapshot files were not restored.",
                    file=sys.stderr,
                )
                return EXIT_COPY_INCOMPLETE
            return EXIT_OK

        if arguments.compact_month is not None:
            get_cutoff_month(arguments.compact_month)

        result = create_backup(
            arguments.project_root,
            arguments.snap_root,
            arguments.source_root,
        )
        if result != EXIT_OK:
            if arguments.compact_day or arguments.compact_month is not None:
                print(
                    "Compaction skipped because the new snapshot is incomplete.",
                    file=sys.stderr,
                )
            return result

        if arguments.compact_day:
            compaction = compact_daily_snapshots(arguments.snap_root)
            print()
            print("Daily compaction complete.")
            print(f"Days compacted:  {compaction.compacted_days}")
            print(f"Snapshots moved: {compaction.moved_snapshots}")
            if compaction.moved_snapshots:
                purge_root = (
                    arguments.snap_root.expanduser().resolve(strict=False)
                    / PURGE_DIRECTORY_NAME
                )
                print(f"Purge:           {purge_root}")

        if arguments.compact_month is not None:
            monthly = compact_monthly_snapshots(
                arguments.snap_root,
                arguments.compact_month,
            )
            print()
            print("Monthly compaction complete.")
            print(f"Cutoff month:             {monthly.cutoff_month}")
            print(
                f"Project-months compacted: "
                f"{monthly.compacted_project_months}"
            )
            print(f"Snapshots moved:          {monthly.moved_snapshots}")
            if monthly.moved_snapshots:
                purge_root = (
                    arguments.snap_root.expanduser().resolve(strict=False)
                    / PURGE_DIRECTORY_NAME
                )
                print(f"Purge:                    {purge_root}")
        return EXIT_OK
    except SnapGitError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as error:
        print(f"ERROR: Operating system error: {error}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("ERROR: Interrupted by user.", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
