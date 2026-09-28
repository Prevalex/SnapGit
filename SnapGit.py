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
import tempfile
import zipfile
import zlib
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_COPY_INCOMPLETE = 2
SNAPSHOT_FORMAT_VERSION = 2
EXIT_UNCHANGED = 3  # Internal result; CLI returns success and skips compaction.
BLOCK_SIZE = 1024 * 1024
STORED_SUFFIXES = {".zip", ".xlsx", ".xlsm", ".docx", ".pptx", ".7z", ".rar",
                   ".gz", ".jpg", ".jpeg", ".png", ".mp3", ".mp4", ".pdf"}
LIST_NAMES = {"tracked.lst", "untracked.lst", "ignored.lst", "backup-files.lst"}
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
class CompactionResult:
    compacted_days: int
    moved_snapshots: int


@dataclass(frozen=True)
class MonthlyCompactionResult:
    cutoff_month: str
    compacted_months: int
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


class ArchiveError(RuntimeError):
    """An archive cannot be created or trusted."""


def checksum_path(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def stream_hash(source, destination=None) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: source.read(BLOCK_SIZE), b""):
        digest.update(block)
        size += len(block)
        if destination is not None:
            destination.write(block)
    return size, digest.hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as source:
        return stream_hash(source)[1]


def stat_signature(value: os.stat_result) -> tuple:
    # Windows path stat and fstat disagree on legacy st_ctime in some Python
    # versions (creation time versus change time). Content hashes remain mandatory.
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns if os.name != "nt" else 0)


def safe_parts(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or "\0" in path:
        raise ArchiveError("Invalid archive path.")
    parts = tuple(path.split("/"))
    if any(p in ("", ".", "..") for p in parts) or path.startswith("/"):
        raise ArchiveError(f"Unsafe archive path: {path!r}")
    if os.name == "nt" and any(
        "\\" in p or ":" in p or p.endswith((" ", "."))
        or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p)
        for p in parts
    ):
        raise ArchiveError(f"Path cannot be safely restored on Windows: {path!r}")
    if parts[0].casefold() == ".git":
        raise ArchiveError("A snapshot must not write into .git.")
    return parts


def member_name(path: str) -> str:
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return "raw-files/" + hashlib.sha256(os.fsencode(path)).hexdigest()
    return "backup/" + path


def inspect_file(root: Path, relative: str, category: str) -> dict:
    path = root.joinpath(*safe_parts(relative))
    before = path.lstat()
    link = os.readlink(path) if stat.S_ISLNK(before.st_mode) else None
    if link is not None:
        data = os.fsencode(link)
        size, digest = len(data), hashlib.sha256(data).hexdigest()
    elif stat.S_ISREG(before.st_mode):
        with path.open("rb") as source:
            if stat_signature(os.fstat(source.fileno())) != stat_signature(before):
                raise ArchiveError(f"File changed while opening: {relative}")
            size, digest = stream_hash(source)
    else:
        raise ArchiveError(f"Unsupported file type: {relative}")
    if stat_signature(path.lstat()) != stat_signature(before):
        raise ArchiveError(f"File changed while hashing: {relative}")
    return {"path": relative, "member": member_name(relative), "category": category,
            "copied": True, "size": size, "sha256": digest,
            "mode": stat.S_IMODE(before.st_mode), "mtime_ns": before.st_mtime_ns,
            "symlink_target": link,
            "symlink_is_directory": path.is_dir() if link is not None else None,
            "_signature": stat_signature(before)}


def validate_manifest(manifest: object) -> dict:
    if not isinstance(manifest, dict) or manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise ArchiveError("Unsupported snapshot format; a version 2 ZIP is required.")
    if manifest.get("status") != "complete":
        raise ArchiveError("Snapshot is not complete.")
    entries = manifest.get("files")
    git = manifest.get("git")
    if not isinstance(entries, list) or not isinstance(git, dict):
        raise ArchiveError("Invalid snapshot manifest.")
    names = set()
    members = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ArchiveError("Invalid file entry.")
        relative = entry.get("path")
        safe_parts(relative)
        key = relative.casefold() if os.name == "nt" else relative
        if key in names:
            raise ArchiveError(f"Duplicate snapshot path: {relative!r}")
        names.add(key)
        member = entry.get("member")
        if member != member_name(relative) or member in members:
            raise ArchiveError(f"Invalid or duplicate ZIP member: {member!r}")
        members.add(member)
        if (entry.get("copied") is not True
                or entry.get("category") not in {"tracked", "untracked", "ignored"}
                or type(entry.get("size")) is not int or entry["size"] < 0
                or not isinstance(entry.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                or type(entry.get("mode")) is not int or not 0 <= entry["mode"] <= 0o7777
                or type(entry.get("mtime_ns")) is not int):
            raise ArchiveError(f"Invalid integrity/metadata fields: {relative!r}")
        target = entry.get("symlink_target")
        if target is not None and (not isinstance(target, str) or "\0" in target):
            raise ArchiveError(f"Invalid symlink: {relative!r}")
    for name in names:
        parts = name.split("/")
        if any("/".join(parts[:i]) in names for i in range(1, len(parts))):
            raise ArchiveError(f"File is also used as a parent directory: {name!r}")
    deleted = git.get("deleted")
    if not isinstance(deleted, list) or len(deleted) != len(set(map(str, deleted))):
        raise ArchiveError("Invalid deleted path list.")
    for relative in deleted:
        safe_parts(relative)
        key = relative.casefold() if os.name == "nt" else relative
        if key in names:
            raise ArchiveError(f"Path is both saved and deleted: {relative!r}")
    return manifest


def verify_archive(path: Path, extraction_root: Path | None = None) -> dict:
    """Verify all bytes and members before a restore can touch its target."""
    try:
        expected = checksum_path(path).read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ArchiveError("Invalid archive SHA-256 file.")
        with path.open("rb") as archive_file:
            before = stat_signature(os.fstat(archive_file.fileno()))
            if stream_hash(archive_file)[1] != expected:
                raise ArchiveError(f"Archive SHA-256 mismatch: {path}")
            archive_file.seek(0)
            with zipfile.ZipFile(archive_file) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if len(names) != len(set(names)):
                    raise ArchiveError("Duplicate ZIP member names.")
                if any(i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                       or i.flag_bits & 1 or i.is_dir() for i in infos):
                    raise ArchiveError("Unsupported ZIP member encoding.")
                manifest = validate_manifest(json.loads(archive.read("snapshot.json")))
                entries = manifest["files"]
                expected_names = {"snapshot.json", *LIST_NAMES,
                                  *(entry["member"] for entry in entries)}
                if set(names) != expected_names:
                    raise ArchiveError("ZIP contents do not match the manifest.")
                # Lists are diagnostic; restoration uses the hashed manifest.
                for name in LIST_NAMES:
                    with archive.open(name) as source:
                        stream_hash(source)
                if extraction_root is not None:
                    (extraction_root / "backup").mkdir(parents=True, exist_ok=True)
                for entry in entries:
                    relative = entry["path"]
                    info = archive.getinfo(entry["member"])
                    if info.file_size != entry["size"]:
                        raise ArchiveError(f"Member size mismatch: {relative}")
                    destination = None
                    if extraction_root is not None and entry["symlink_target"] is None:
                        destination = (extraction_root / "backup").joinpath(*safe_parts(relative))
                        destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source:
                        if destination is not None:
                            with destination.open("xb") as output:
                                size, digest = stream_hash(source, output)
                        else:
                            size, digest = stream_hash(source)
                    if size != entry["size"] or digest != entry["sha256"]:
                        raise ArchiveError(f"Member SHA-256 mismatch: {relative}")
                    target = entry["symlink_target"]
                    if target is not None:
                        data = os.fsencode(target)
                        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                            raise ArchiveError(f"Symlink target mismatch: {relative}")
                    elif destination is not None:
                        os.utime(destination, ns=(entry["mtime_ns"], entry["mtime_ns"]))
                if stat_signature(os.fstat(archive_file.fileno())) != before:
                    raise ArchiveError("Archive changed during verification.")
                if extraction_root is not None:
                    # No links exist while writing regular files to staging.
                    for entry in entries:
                        if entry["symlink_target"] is not None:
                            dest = (extraction_root / "backup").joinpath(*safe_parts(entry["path"]))
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            os.symlink(entry["symlink_target"], dest,
                                       target_is_directory=bool(entry["symlink_is_directory"]))
                    (extraction_root / "snapshot.json").write_text(
                        json.dumps(manifest, ensure_ascii=True), encoding="utf-8")
                return manifest
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, zipfile.BadZipFile,
            NotImplementedError, RuntimeError, EOFError, zlib.error) as error:
        if isinstance(error, ArchiveError):
            raise
        raise ArchiveError(f"Could not verify {path}: {error}") from error


def write_archive(path: Path, manifest: dict, root: Path, lists: dict,
                  check_source) -> None:
    """Publish the ZIP last, only after fsync and a full independent readback."""
    temporary = path.with_name(path.name + ".partial")
    temporary_checksum = checksum_path(temporary)
    final_checksum = checksum_path(path)
    published_checksum = False
    created = False
    checksum_created = False
    try:
        validate_manifest(manifest)
        if path.exists() or final_checksum.exists():
            raise ArchiveError(f"Snapshot already exists: {path}")
        with temporary.open("xb") as output:
            created = True
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=9, allowZip64=True) as archive:
                for entry in manifest["files"]:
                    source_path = root.joinpath(*safe_parts(entry["path"]))
                    signature = entry["_signature"]
                    if stat_signature(source_path.lstat()) != signature:
                        raise ArchiveError(f"File changed before archiving: {entry['path']}")
                    stamp = datetime.fromtimestamp(entry["mtime_ns"] / 1_000_000_000)
                    zip_stamp = (min(2107, max(1980, stamp.year)), stamp.month,
                                 stamp.day, stamp.hour, stamp.minute, stamp.second)
                    info = zipfile.ZipInfo(entry["member"], date_time=zip_stamp)
                    info.create_system = 3
                    link = entry["symlink_target"]
                    mode = stat.S_IFLNK if link is not None else stat.S_IFREG
                    info.external_attr = (mode | entry["mode"]) << 16
                    info.compress_type = (zipfile.ZIP_STORED if link is not None
                                          or source_path.suffix.lower() in STORED_SUFFIXES
                                          else zipfile.ZIP_DEFLATED)
                    info._compresslevel = 9
                    info.file_size = entry["size"]
                    with archive.open(info, "w", force_zip64=entry["size"] >= zipfile.ZIP64_LIMIT) as dest:
                        if link is not None:
                            data = os.fsencode(os.readlink(source_path))
                            dest.write(data)
                            size, digest = len(data), hashlib.sha256(data).hexdigest()
                        else:
                            with source_path.open("rb") as source:
                                if stat_signature(os.fstat(source.fileno())) != signature:
                                    raise ArchiveError(f"File changed while opening: {entry['path']}")
                                size, digest = stream_hash(source, dest)
                    if (size != entry["size"] or digest != entry["sha256"]
                            or stat_signature(source_path.lstat()) != signature):
                        raise ArchiveError(f"File changed while archiving: {entry['path']}")
                for name, paths in lists.items():
                    archive.writestr(name, ("\n".join(paths) + "\n").encode("utf-8", "surrogateescape"))
                saved_manifest = dict(manifest)
                saved_manifest["files"] = [{k: v for k, v in e.items() if not k.startswith("_")}
                                           for e in manifest["files"]]
                archive.writestr("snapshot.json", json.dumps(saved_manifest, ensure_ascii=True, indent=2))
            output.flush()
            os.fsync(output.fileno())
        with temporary_checksum.open("x", encoding="ascii") as output:
            checksum_created = True
            output.write(file_hash(temporary) + "\n")
            output.flush()
            os.fsync(output.fileno())
        print("Verifying ZIP CRC, file SHA-256 and archive SHA-256...")
        verify_archive(temporary)
        check_source()
        if path.exists() or final_checksum.exists():
            raise ArchiveError(f"Snapshot already exists: {path}")
        temporary_checksum.rename(final_checksum)
        published_checksum = True
        temporary.rename(path)
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, ArchiveError):
            raise
        raise ArchiveError(f"Could not create {path}: {error}") from error
    finally:
        if created:
            temporary.unlink(missing_ok=True)
        if checksum_created:
            temporary_checksum.unlink(missing_ok=True)
        if published_checksum and not path.exists():
            final_checksum.unlink(missing_ok=True)


def parse_keep_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("keep count must be an integer >= 1") from error
    if count < 1:
        raise argparse.ArgumentTypeError("keep count must be an integer >= 1")
    return count


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
            "After a successful backup, keep only the latest snapshot for the "
            "current project and each day, moving older snapshots below "
            "SnapRoot/$purge$, except snapshots protected by --keep."
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
            "For the current project and every month up to the selected cutoff "
            "month, keep only the latest snapshot, subject to --keep. Use 0 for the current month, "
            "-1 for the previous month, and so on. When omitted, MONTH_OFFSET "
            "defaults to -6."
        ),
    )

    backup_parser.add_argument(
        "--keep",
        "-k",
        type=parse_keep_count,
        default=1,
        metavar="N",
        help=(
            "Protect the N latest snapshots of this project across all dates "
            "from both -cd and -cm, including the new snapshot. "
            "This is a global minimum, not a per-period count or total maximum. "
            "Integer >= 1; default: 1. "
            "Has no effect without -cd or -cm."
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
        help="Snapshot ZIP file with its companion .zip.sha256 file.",
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
    verify_parser = command_parsers.add_parser(
        "verify", help="Fully verify a ZIP snapshot without restoring it."
    )
    verify_parser.add_argument("snapshot", type=Path, metavar="SNAPSHOT_ZIP")

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


def read_snapshot_manifest(snapshot_root: Path) -> dict[str, object]:
    manifest_path = snapshot_root / "snapshot.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SnapGitError(
            "Verified staging directory is missing snapshot.json."
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def restore_regular_file(source: Path, destination: Path, entry: dict) -> None:
    """Check a staged replacement before atomically replacing the destination."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=".snapgit-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as original:
            shutil.copyfileobj(original, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if temporary.stat().st_size != entry["size"] or sha256_file(temporary) != entry["sha256"]:
            raise OSError(f"Restored file failed SHA-256 verification: {destination}")
        os.utime(temporary, ns=(entry["mtime_ns"], entry["mtime_ns"]))
        os.chmod(temporary, entry["mode"])
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            os.chmod(temporary, 0o600)
            temporary.unlink()


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


def create_snapshot_root(snap_root: Path, project_snapshot_path: Path) -> Path:
    now = datetime.now()
    day = snap_root / project_snapshot_path / now.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    path = day / (now.strftime("%H-%M-%S") + ".zip")
    if path.exists() or checksum_path(path).exists():
        raise SnapGitError(f"Backup destination already exists: {path}. Retry in a second.")
    return path


def find_snapshot_days(project_root: Path) -> Iterable[tuple[Path, list[Path]]]:
    # Only immediate date directories belonging to this project; never recurse
    # into another project's archive or $purge$.
    if not project_root.is_dir():
        return
    for day in sorted(project_root.iterdir()):
        if day.is_symlink() or not day.is_dir() or not DATE_DIRECTORY_PATTERN.fullmatch(day.name):
            continue
        snapshots = [p for p in day.iterdir() if p.is_file() and not p.is_symlink()
                     and p.suffix == ".zip" and TIME_DIRECTORY_PATTERN.fullmatch(p.stem)]
        if snapshots:
            yield day, snapshots


def project_snapshots(project_root: Path) -> list[Path]:
    return sorted((p for _, paths in find_snapshot_days(project_root) for p in paths),
                  key=lambda p: (p.parent.name, p.name))


@contextmanager
def project_archive_lock(project_root: Path):
    project_root.mkdir(parents=True, exist_ok=True)
    lock = project_root / ".snapgit.lock"
    try:
        handle = lock.open("x", encoding="ascii")
    except FileExistsError as error:
        raise SnapGitError(
            f"Project archive is locked: {lock}. If a previous run crashed, "
            "remove this lock only after confirming no SnapGit process is running."
        ) from error
    try:
        with handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def move_snapshots_to_purge(
    snap_root: Path, snapshots: Iterable[Path]
) -> int:
    purge_root = snap_root / PURGE_DIRECTORY_NAME
    moves: list[tuple[Path, Path]] = []
    for snapshot in snapshots:
        for source in (snapshot, checksum_path(snapshot)):
            destination = purge_root / source.relative_to(snap_root)
            if destination.exists():
                raise SnapGitError(f"Compaction would overwrite a purge file: {destination}")
            if not source.is_file():
                raise SnapGitError(f"Compaction requires the archive and checksum: {source}")
            moves.append((source, destination))
    completed = []
    try:
        for source, destination in moves:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            completed.append((source, destination))
    except OSError as error:
        for source, destination in reversed(completed):
            destination.rename(source)
        raise SnapGitError(f"Could not compact snapshots: {error}") from error
    return len(moves) // 2


def resolve_compaction_scope(
    snap_root_argument: Path,
    project_snapshot_path: Path,
) -> tuple[Path, Path]:
    snap_root = snap_root_argument.expanduser().resolve(strict=False)
    if not snap_root.is_dir():
        raise SnapGitError(
            f"SnapRoot does not exist or is not a directory: {snap_root}"
        )

    if (
        project_snapshot_path.is_absolute()
        or not project_snapshot_path.parts
        or any(part in ("", ".", "..") for part in project_snapshot_path.parts)
    ):
        raise SnapGitError(
            f"Invalid project snapshot path: {project_snapshot_path}"
        )
    project_snap_root = (snap_root / project_snapshot_path).resolve(strict=False)
    if not is_same_or_within(project_snap_root, snap_root):
        raise SnapGitError(
            f"Project snapshot path is outside SnapRoot: {project_snap_root}"
        )
    if not project_snap_root.is_dir():
        raise SnapGitError(
            "Project snapshot directory does not exist: "
            f"{project_snap_root}"
        )
    return snap_root, project_snap_root


def compact_daily_snapshots(
    snap_root_argument: Path,
    project_snapshot_path: Path,
    *,
    keep: int = 1,
) -> CompactionResult:
    if type(keep) is not int or keep < 1:
        raise SnapGitError("Keep count must be an integer >= 1.")
    snap_root, project_snap_root = resolve_compaction_scope(
        snap_root_argument,
        project_snapshot_path,
    )

    all_snapshots = project_snapshots(project_snap_root)
    if len(all_snapshots) <= keep:
        print(f"Compaction skipped: the project has at most {keep} snapshots.")
        return CompactionResult(0, 0)
    protected = set(all_snapshots[-keep:])
    snapshots_to_move: list[Path] = []
    survivors: list[Path] = []
    compacted_days = 0

    for _, snapshots in find_snapshot_days(project_snap_root):
        ordered = sorted(snapshots, key=lambda path: path.name)
        to_move = [path for path in ordered[:-1] if path not in protected]
        if not to_move:
            continue

        compacted_days += 1
        survivors.extend(path for path in ordered if path == ordered[-1] or path in protected)
        snapshots_to_move.extend(to_move)

    for survivor in survivors:
        verify_archive(survivor)
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
    project_snapshot_path: Path,
    month_offset: int,
    now: datetime | None = None,
    *,
    keep: int = 1,
) -> MonthlyCompactionResult:
    if type(keep) is not int or keep < 1:
        raise SnapGitError("Keep count must be an integer >= 1.")
    snap_root, project_snap_root = resolve_compaction_scope(
        snap_root_argument,
        project_snapshot_path,
    )

    cutoff_month = get_cutoff_month(month_offset, now)
    all_snapshots = project_snapshots(project_snap_root)
    if len(all_snapshots) <= keep:
        print(f"Compaction skipped: the project has at most {keep} snapshots.")
        return MonthlyCompactionResult(cutoff_month, 0, 0)
    protected = set(all_snapshots[-keep:])
    monthly_snapshots: dict[str, list[Path]] = {}
    for date_path, snapshots in find_snapshot_days(project_snap_root):
        snapshot_month = date_path.name[:7]
        if snapshot_month > cutoff_month:
            continue
        monthly_snapshots.setdefault(snapshot_month, []).extend(snapshots)

    snapshots_to_move: list[Path] = []
    compacted_months = 0
    survivors: list[Path] = []
    for snapshots in monthly_snapshots.values():
        ordered = sorted(
            snapshots,
            key=lambda path: (path.parent.name, path.name),
        )
        to_move = [path for path in ordered[:-1] if path not in protected]
        if not to_move:
            continue
        compacted_months += 1
        survivors.extend(path for path in ordered if path == ordered[-1] or path in protected)
        snapshots_to_move.extend(to_move)

    for survivor in survivors:
        verify_archive(survivor)
    moved_snapshots = move_snapshots_to_purge(snap_root, snapshots_to_move)
    return MonthlyCompactionResult(
        cutoff_month=cutoff_month,
        compacted_months=compacted_months,
        moved_snapshots=moved_snapshots,
    )


def _restore_extra_files(
    project_root_argument: Path,
    snapshot_argument: Path,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
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

    manifest = read_snapshot_manifest(snapshot_root)
    backup_root = snapshot_root / "backup"
    entries = {entry["path"]: entry for entry in manifest["files"]}
    candidates = [path for path, entry in entries.items()
                  if entry["category"] in {"untracked", "ignored"}]

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
            source_is_file = source.is_file() or source.is_symlink()
        except OSError as error:
            failed += 1
            errors.append(f"FAILED: {relative_path} :: {error}")
            continue
        if not source_is_file:
            missing += 1
            errors.append(f"MISSING: {relative_path}")
            continue

        try:
            resolved_source = source.parent.resolve(strict=True) / source.name
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
                entry = entries[item.relative_path]
                if entry["symlink_target"] is not None:
                    if os.path.lexists(item.destination):
                        item.destination.unlink()
                    os.symlink(entry["symlink_target"], item.destination,
                               target_is_directory=bool(entry["symlink_is_directory"]))
                else:
                    restore_regular_file(item.source, item.destination, entry)
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


def _restore_full_snapshot(
    project_root_argument: Path,
    snapshot_argument: Path,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
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
            if not is_same_or_within(item.destination.parent.resolve(), project_root):
                raise OSError("Destination parent resolves outside ProjectRoot after checkout")
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
                restore_regular_file(item.source, item.destination, entry)
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


def restore_extra_files(project_root_argument: Path, snapshot_argument: Path, **options) -> RestoreResult:
    return restore_verified(project_root_argument, snapshot_argument, full=False, **options)


def restore_full_snapshot(project_root_argument: Path, snapshot_argument: Path, **options) -> FullRestoreResult:
    return restore_verified(project_root_argument, snapshot_argument, full=True, **options)


def restore_verified(project: Path, snapshot: Path, *, full: bool, **options):
    project = resolve_restore_project_root(project)
    snapshot = snapshot.expanduser().resolve(strict=True)
    if not snapshot.is_file() or snapshot.suffix.lower() != ".zip":
        raise SnapGitError("Restore requires a ZIP snapshot, not a legacy directory.")
    if is_same_or_within(snapshot, project):
        raise SnapGitError("Snapshot must be outside the restore target project.")
    print("Verifying the entire snapshot before restoring...")
    # A private staging directory keeps unverified bytes away from the project.
    # It also permits full validation before checkout changes the target tree.
    with tempfile.TemporaryDirectory(prefix="snapgit-restore-") as temporary:
        root = Path(temporary)
        verify_archive(snapshot, root)
        restore = _restore_full_snapshot if full else _restore_extra_files
        return restore(project, root, **options)


def collect_project(project_root: Path) -> tuple[dict, dict, dict]:
    ignore_case = get_git_ignore_case(project_root)
    tracked = get_git_null_list(project_root, "ls-files")
    untracked = get_git_null_list(project_root, "ls-files", "--others", "--exclude-standard")
    ignored = get_git_null_list(project_root, "ls-files", "--others", "--ignored", "--exclude-standard")
    git_metadata = get_git_snapshot_metadata(project_root, ignore_case=ignore_case)
    rules = read_snap_ignore(project_root / ".snapignore", ignore_case=ignore_case)
    # Excluded paths must not trigger snapshots through Git status metadata.
    for key in ("staged", "unstaged", "modified", "deleted"):
        git_metadata[key] = sorted(p for p in git_metadata[key] if is_included(p, rules))
    deleted_keys = {relative_path_key(p, ignore_case=ignore_case) for p in git_metadata["deleted"]}
    categories = {}
    for category, paths in (("tracked", tracked), ("untracked", untracked), ("ignored", ignored)):
        for path in paths:
            if is_included(path, rules) and relative_path_key(path, ignore_case=ignore_case) not in deleted_keys:
                categories.setdefault(path, category)
    categories = dict(sorted(categories.items()))
    return git_metadata, categories, {
        "tracked.lst": tracked, "untracked.lst": untracked,
        "ignored.lst": ignored, "backup-files.lst": list(categories),
    }


def snapshot_identity(manifest: dict) -> dict:
    fields = ("path", "category", "size", "sha256", "mode", "symlink_target", "symlink_is_directory")
    return {"git": manifest["git"],
            "files": [{key: entry[key] for key in fields} for entry in manifest["files"]]}


def create_backup(project_root_argument: Path, snap_root_argument: Path,
                  source_root_argument: Path | None = None) -> int:
    project_root = resolve_project_root(project_root_argument)
    source_root = resolve_source_root(source_root_argument, project_root)
    project_path = get_project_snapshot_path(project_root, source_root)
    snap_root = resolve_snap_root(snap_root_argument, project_root, project_path)
    print(f"Project: {project_root}")
    print("Reading and hashing selected files...")
    git_metadata, categories, lists = collect_project(project_root)
    entries = [inspect_file(project_root, path, category) for path, category in categories.items()]
    manifest = {
        "format_version": SNAPSHOT_FORMAT_VERSION, "status": "complete",
        "created_at": datetime.now().astimezone().isoformat(),
        "project": {"name": project_root.name, "relative_path": project_path.as_posix(),
                    "source_root": str(source_root) if source_root else None,
                    "original_root": str(project_root)},
        "git": git_metadata, "files": entries,
    }

    def check_source():
        current_git, current_categories, _ = collect_project(project_root)
        if current_git != git_metadata or current_categories != categories:
            raise ArchiveError("Project file list or Git state changed during backup. Retry when idle.")
        for entry in entries:
            source = project_root.joinpath(*safe_parts(entry["path"]))
            if stat_signature(source.lstat()) != entry["_signature"]:
                raise ArchiveError(f"File changed during backup: {entry['path']}. Retry when idle.")

    previous = project_snapshots(snap_root / project_path)
    if previous:
        print(f"Verifying latest snapshot: {previous[-1]}")
        try:
            latest = verify_archive(previous[-1])
        except ArchiveError as error:
            print(f"WARNING: Latest snapshot is not usable: {error}. Creating a replacement.", file=sys.stderr)
        else:
            if snapshot_identity(latest) == snapshot_identity(manifest):
                check_source()
                print("No changes: verified latest snapshot matches the project. Backup and compaction skipped.")
                return EXIT_UNCHANGED
    destination = create_snapshot_root(snap_root, project_path)
    print(f"Writing ZIP: {destination}")
    write_archive(destination, manifest, project_root, lists, check_source)
    print(f"Backup verified: {destination}")
    print(f"Files: {len(entries)}; source bytes: {sum(e['size'] for e in entries)}; ZIP bytes: {destination.stat().st_size}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    try:
        if arguments.command == "verify":
            manifest = verify_archive(arguments.snapshot.expanduser().resolve(strict=True))
            print(f"Verification OK: {len(manifest['files'])} files; ZIP CRC and SHA-256 checks passed.")
            return EXIT_OK
        if arguments.command == "restore":
            if arguments.mode == "full":
                full_result = restore_full_snapshot(
                    arguments.project_root,
                    arguments.snapshot,
                    dry_run=arguments.dry_run,
                    overwrite=arguments.overwrite,
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

        project_root = resolve_project_root(arguments.project_root)
        source_root = resolve_source_root(arguments.source_root, project_root)
        project_snapshot_path = get_project_snapshot_path(
            project_root,
            source_root,
        )

        archive_root = resolve_snap_root(arguments.snap_root, project_root, project_snapshot_path)
        with project_archive_lock(archive_root / project_snapshot_path):
            result = create_backup(
                project_root,
                arguments.snap_root,
                source_root,
            )
            if result == EXIT_UNCHANGED:
                return EXIT_OK
            if result != EXIT_OK:
                if arguments.compact_day or arguments.compact_month is not None:
                    print(
                        "Compaction skipped because the new snapshot is incomplete.",
                        file=sys.stderr,
                    )
                return result

            if arguments.compact_day:
                compaction = compact_daily_snapshots(
                    arguments.snap_root,
                    project_snapshot_path,
                    keep=arguments.keep,
                )
                print()
                print("Daily compaction complete.")
                print(f"Protected latest: {arguments.keep}")
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
                    project_snapshot_path,
                    arguments.compact_month,
                    keep=arguments.keep,
                )
                print()
                print("Monthly compaction complete.")
                print(f"Protected latest:         {arguments.keep}")
                print(f"Cutoff month:             {monthly.cutoff_month}")
                print(
                    f"Months compacted:         "
                    f"{monthly.compacted_months}"
                )
                print(f"Snapshots moved:          {monthly.moved_snapshots}")
                if monthly.moved_snapshots:
                    purge_root = (
                        arguments.snap_root.expanduser().resolve(strict=False)
                        / PURGE_DIRECTORY_NAME
                    )
                    print(f"Purge:                    {purge_root}")
        return EXIT_OK
    except (SnapGitError, ArchiveError) as error:
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
