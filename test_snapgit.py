"""Integration and failure-injection checks for backup integrity and retention."""

import contextlib
import io
import json
import os
import signal
import stat
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import SnapGit as sg


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"SNAP_SOURCE_ROOT": "", "SNAP_ROOT": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="snapgit-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.archives = self.root / "archives"
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Snapshot Test")
        self.git("config", "core.autocrlf", "false")
        (self.repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (self.repo / ".snapignore").write_text("excluded/\n", encoding="utf-8")
        (self.repo / "tracked.txt").write_text("original", encoding="utf-8")
        (self.repo / "delete.txt").write_text("delete me", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.second = 0
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", "-C", str(cwd or self.repo), *args], stderr=subprocess.PIPE)

    def backup(self):
        self.second += 1
        with patch.object(sg, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 28, 12, 0, self.second)
            result = sg.create_backup(self.repo, self.archives)
        return result, sg.project_snapshots(self.archives / "project")[-1]

    def clone(self):
        destination = self.root / "clone"
        self.git("clone", "-q", str(self.repo), str(destination))
        return destination

    def duplicate_snapshot(self, source, day, time):
        destination = self.archives / "project" / day / (time + ".zip")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        shutil.copy2(sg.checksum_path(source), sg.checksum_path(destination))
        return destination

    def rewrite(self, archive, mutate):
        with zipfile.ZipFile(archive) as source:
            members = {i.filename: source.read(i) for i in source.infolist()}
        mutate(members)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as dest:
            for name, data in members.items():
                dest.writestr(name, data)
        sg.checksum_path(archive).write_text(sg.file_hash(archive) + "\n", encoding="ascii")

    def test_zip_and_unchanged_including_timestamp_only(self):
        result, archive = self.backup()
        self.assertEqual(result, sg.EXIT_OK)
        manifest = sg.verify_archive(archive)
        self.assertEqual(len(manifest["files"]), 4)
        os.utime(self.repo / "tracked.txt", None)
        self.assertEqual(self.backup()[0], sg.EXIT_UNCHANGED)
        self.assertEqual(len(sg.project_snapshots(self.archives / "project")), 1)

    def test_single_file_runs_without_repository_modules(self):
        standalone = self.root / "SnapGit.py"
        shutil.copy2(Path(sg.__file__), standalone)
        (self.repo / "extra.txt").write_text("standalone backup", encoding="utf-8")
        def run(*args):
            result = subprocess.run(
                [sys.executable, "-I", str(standalone), *map(str, args)],
                cwd=self.root, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout
        run("backup", self.repo, "-sr", self.archives)
        archive = sg.project_snapshots(self.archives / "project")[-1]
        run("verify", archive)
        self.assertIn("No changes", run("backup", self.repo, "-sr", self.archives))
        target = self.clone()
        run("restore", target, "--snapshot", archive)
        self.assertEqual((target / "extra.txt").read_text(), "standalone backup")

    def test_same_size_same_mtime_content_change(self):
        self.backup()
        path = self.repo / "tracked.txt"
        before = path.stat()
        path.write_text("modified", encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.backup()[0], sg.EXIT_OK)

    def test_executable_extensions_backup_verify_and_restore(self):
        payloads = {"tool.cmd": b"@echo off\r\n", "tool.bat": b"echo test\r\n",
                    "tool.exe": b"test executable payload", "tool.com": b"test COM payload"}
        for name, data in payloads.items():
            (self.repo / name).write_bytes(data)
        _, archive = self.backup()
        sg.verify_archive(archive)
        self.assertEqual(self.backup()[0], sg.EXIT_UNCHANGED)
        target = self.clone()
        result = sg.restore_extra_files(target, archive)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.restored, len(payloads))
        for name, data in payloads.items():
            self.assertEqual((target / name).read_bytes(), data)

    def test_add_remove_and_ignored_files(self):
        self.backup()
        (self.repo / "ignored").mkdir()
        path = self.repo / "ignored" / "данные.json"
        path.write_text('{"value": 42}', encoding="utf-8")
        self.assertEqual(self.backup()[0], sg.EXIT_OK)
        path.unlink()
        self.assertEqual(self.backup()[0], sg.EXIT_OK)

    def test_excluded_changes_do_not_trigger(self):
        (self.repo / "excluded").mkdir()
        path = self.repo / "excluded" / "tracked.txt"
        path.write_text("one", encoding="utf-8")
        self.git("add", "excluded")
        self.git("commit", "-qm", "excluded tracked file")
        self.backup()
        path.write_text("two", encoding="utf-8")
        (self.repo / "excluded" / "extra").write_text("new", encoding="utf-8")
        self.assertEqual(self.backup()[0], sg.EXIT_UNCHANGED)

    def test_git_commit_and_staging_changes_trigger(self):
        (self.repo / "tracked.txt").write_text("changed", encoding="utf-8")
        self.backup()
        self.git("add", "tracked.txt")
        self.assertEqual(self.backup()[0], sg.EXIT_OK)
        self.git("commit", "-qm", "change")
        self.assertEqual(self.backup()[0], sg.EXIT_OK)

    def test_corrupt_latest_creates_replacement(self):
        _, archive = self.backup()
        with archive.open("ab") as out:
            out.write(b"damage")
        with self.assertRaises(sg.ArchiveError):
            sg.verify_archive(archive)
        self.assertEqual(self.backup()[0], sg.EXIT_OK)

    def test_missing_checksum_fails_verify(self):
        _, archive = self.backup()
        sg.checksum_path(archive).unlink()
        self.assertEqual(sg.main(["verify", str(archive)]), sg.EXIT_ERROR)

    def test_member_hash_catches_repacked_corruption(self):
        _, archive = self.backup()
        self.rewrite(archive, lambda m: m.__setitem__("backup/tracked.txt", b"tampered"))
        with self.assertRaisesRegex(sg.ArchiveError, "SHA-256"):
            sg.verify_archive(archive)

    def test_zip_crc_checked_even_with_updated_archive_checksum(self):
        (self.repo / "stored.xlsx").write_bytes(b"stored payload for CRC test")
        _, archive = self.backup()
        with zipfile.ZipFile(archive) as source:
            info = source.getinfo("backup/stored.xlsx")
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
        with archive.open("r+b") as output:
            output.seek(info.header_offset + 26)
            name_length, extra_length = struct.unpack("<HH", output.read(4))
            output.seek(info.header_offset + 30 + name_length + extra_length)
            output.write(b"X")
        sg.checksum_path(archive).write_text(sg.file_hash(archive) + "\n", encoding="ascii")
        with self.assertRaisesRegex(sg.ArchiveError, "CRC"):
            sg.verify_archive(archive)

    def test_truncated_archive_fails(self):
        _, archive = self.backup()
        with archive.open("r+b") as out:
            out.truncate(archive.stat().st_size - 32)
        with self.assertRaises(sg.ArchiveError):
            sg.verify_archive(archive)

    def test_manifest_and_unlisted_member_rejected(self):
        _, archive = self.backup()
        self.rewrite(archive, lambda m: m.__setitem__("extra.txt", b"unexpected"))
        with self.assertRaisesRegex(sg.ArchiveError, "contents"):
            sg.verify_archive(archive)

    def test_unsafe_manifest_path_rejected(self):
        _, archive = self.backup()
        def mutate(members):
            manifest = json.loads(members["snapshot.json"])
            manifest["files"][0]["path"] = "../escape"
            members["snapshot.json"] = json.dumps(manifest).encode()
        self.rewrite(archive, mutate)
        with self.assertRaises(sg.ArchiveError):
            sg.verify_archive(archive)

    def test_restore_extras_and_full(self):
        (self.repo / "tracked.txt").write_text("local changes", encoding="utf-8")
        (self.repo / "delete.txt").unlink()
        (self.repo / "данные.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        _, archive = self.backup()
        target = self.clone()
        dry = sg.restore_full_snapshot(target, archive, dry_run=True)
        self.assertEqual(dry.planned_deletions, 1)
        self.assertEqual((target / "tracked.txt").read_text(), "original")
        result = sg.restore_full_snapshot(target, archive)
        self.assertEqual(result.failed, 0)
        self.assertEqual((target / "tracked.txt").read_text(), "local changes")
        self.assertFalse((target / "delete.txt").exists())
        (target / "данные.csv").unlink()
        result = sg.restore_extra_files(target, archive)
        self.assertEqual(result.restored, 1)
        self.assertEqual((target / "данные.csv").read_bytes(), (self.repo / "данные.csv").read_bytes())

    def test_extras_never_overwrites_tracked(self):
        (self.repo / "extra.txt").write_text("snapshot", encoding="utf-8")
        _, archive = self.backup()
        target = self.clone()
        (target / "extra.txt").write_text("target", encoding="utf-8")
        self.git("add", "extra.txt", cwd=target)
        result = sg.restore_extra_files(target, archive, overwrite=True)
        self.assertEqual(result.tracked_conflicts, 1)
        self.assertEqual((target / "extra.txt").read_text(), "target")

    def test_corruption_in_tracked_member_blocks_extras_and_full_before_writes(self):
        (self.repo / "extra.txt").write_text("extra", encoding="utf-8")
        _, archive = self.backup()
        target = self.clone()
        head = self.git("rev-parse", "HEAD", cwd=target)
        self.rewrite(archive, lambda m: m.__setitem__("backup/tracked.txt", b"tampered"))
        for restore in (sg.restore_extra_files, sg.restore_full_snapshot):
            with self.assertRaises(sg.ArchiveError):
                restore(target, archive)
            self.assertFalse((target / "extra.txt").exists())
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=target), head)

    def test_single_snapshot_never_compacted(self):
        _, archive = self.backup()
        with patch.object(sg, "move_snapshots_to_purge") as move:
            self.assertEqual(sg.compact_daily_snapshots(self.archives, Path("project")).moved_snapshots, 0)
            self.assertEqual(sg.compact_monthly_snapshots(self.archives, Path("project"), 0).moved_snapshots, 0)
            move.assert_not_called()
        self.assertTrue(archive.exists())
        self.assertTrue(sg.checksum_path(archive).exists())

    def test_daily_keep_protects_global_latest_only(self):
        _, first = self.backup()
        second = self.duplicate_snapshot(first, "2026-09-28", "13-00-00")
        latest = self.duplicate_snapshot(first, "2026-09-28", "14-00-00")
        other_day = self.duplicate_snapshot(first, "2026-09-27", "12-00-00")
        older_same_day = self.duplicate_snapshot(first, "2026-09-27", "11-00-00")
        result = sg.compact_daily_snapshots(self.archives, Path("project"), keep=2)
        self.assertEqual(result.moved_snapshots, 2)
        self.assertFalse(older_same_day.exists())
        self.assertEqual(set(sg.project_snapshots(self.archives / "project")), {second, latest, other_day})
        purged = self.archives / "$purge$" / first.relative_to(self.archives)
        sg.verify_archive(purged)

    def test_monthly_global_keep_includes_dates_after_cutoff(self):
        _, latest = self.backup()
        first = self.duplicate_snapshot(latest, "2026-09-01", "12-00-00")
        second = self.duplicate_snapshot(latest, "2026-09-02", "12-00-00")
        future1 = self.duplicate_snapshot(latest, "2026-10-01", "12-00-00")
        future2 = self.duplicate_snapshot(latest, "2026-10-02", "12-00-00")
        future3 = self.duplicate_snapshot(latest, "2026-10-03", "12-00-00")
        result = sg.compact_monthly_snapshots(
            self.archives, Path("project"), 0, now=datetime(2026, 9, 28), keep=2,
        )
        self.assertEqual(result.moved_snapshots, 2)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(set(sg.project_snapshots(self.archives / "project")),
                         {latest, future1, future2, future3})

    def test_keep_above_available_count_never_moves(self):
        _, first = self.backup()
        self.duplicate_snapshot(first, "2026-09-28", "13-00-00")
        with patch.object(sg, "move_snapshots_to_purge") as move:
            for keep in (2, 3, 10**40):
                self.assertEqual(sg.compact_daily_snapshots(self.archives, Path("project"), keep=keep).moved_snapshots, 0)
                self.assertEqual(sg.compact_monthly_snapshots(self.archives, Path("project"), 0, keep=keep).moved_snapshots, 0)
            move.assert_not_called()

    def test_all_retained_snapshots_verified_before_compaction(self):
        _, first = self.backup()
        second = self.duplicate_snapshot(first, "2026-09-28", "13-00-00")
        self.duplicate_snapshot(first, "2026-09-28", "14-00-00")
        with second.open("ab") as output:
            output.write(b"damage in retained older snapshot")
        for compact in (
            lambda: sg.compact_daily_snapshots(self.archives, Path("project"), keep=2),
            lambda: sg.compact_monthly_snapshots(self.archives, Path("project"), 0, now=datetime(2026, 9, 28), keep=2),
        ):
            with self.assertRaises(sg.ArchiveError):
                compact()
            self.assertTrue(first.exists())

    def test_combined_daily_monthly_keep_count(self):
        _, source = self.backup()
        for day in ("2026-09-27", "2026-09-28"):
            for time in ("13-00-00", "14-00-00", "15-00-00"):
                self.duplicate_snapshot(source, day, time)
        sg.compact_daily_snapshots(self.archives, Path("project"), keep=2)
        sg.compact_monthly_snapshots(self.archives, Path("project"), 0, now=datetime(2026, 9, 28), keep=2)
        remaining = sg.project_snapshots(self.archives / "project")
        self.assertEqual([(p.parent.name, p.stem) for p in remaining],
                         [("2026-09-28", "14-00-00"), ("2026-09-28", "15-00-00")])

    def test_combined_compaction_protects_latest_across_month_boundary(self):
        _, newest = self.backup()
        protected1 = self.duplicate_snapshot(newest, "2026-08-31", "13-00-00")
        protected2 = self.duplicate_snapshot(newest, "2026-08-31", "14-00-00")
        old1 = self.duplicate_snapshot(newest, "2026-08-31", "12-00-00")
        old2 = self.duplicate_snapshot(newest, "2026-08-30", "12-00-00")
        historical = self.duplicate_snapshot(newest, "2026-07-31", "12-00-00")
        old3 = self.duplicate_snapshot(newest, "2026-07-30", "12-00-00")
        latest_before = sg.project_snapshots(self.archives / "project")[-3:]
        sg.compact_daily_snapshots(self.archives, Path("project"), keep=3)
        sg.compact_monthly_snapshots(self.archives, Path("project"), 0, now=datetime(2026, 9, 28), keep=3)
        remaining = sg.project_snapshots(self.archives / "project")
        self.assertEqual(remaining[-3:], latest_before)
        self.assertEqual(set(remaining), {historical, protected1, protected2, newest})
        self.assertGreater(len(remaining), 3)  # A minimum, not a total maximum.
        for old in (old1, old2, old3):
            self.assertFalse(old.exists())

    def test_monthly_keep_preserves_latest_beyond_normal_period_survivor(self):
        _, newest = self.backup()
        second = self.duplicate_snapshot(newest, "2026-09-27", "12-00-00")
        third = self.duplicate_snapshot(newest, "2026-09-26", "12-00-00")
        old = self.duplicate_snapshot(newest, "2026-09-25", "12-00-00")
        sg.compact_monthly_snapshots(self.archives, Path("project"), 0, now=datetime(2026, 9, 28), keep=3)
        self.assertEqual(set(sg.project_snapshots(self.archives / "project")), {third, second, newest})
        self.assertFalse(old.exists())

    def test_cli_passes_keep_to_both_compactions(self):
        with patch.object(sg, "compact_daily_snapshots", wraps=sg.compact_daily_snapshots) as daily, patch.object(sg, "compact_monthly_snapshots", wraps=sg.compact_monthly_snapshots) as monthly:
            self.assertEqual(sg.main(["backup", str(self.repo), "-sr", str(self.archives), "-cd", "-cm", "0", "-k", "3"]), 0)
            self.assertEqual(daily.call_args.kwargs["keep"], 3)
            self.assertEqual(monthly.call_args.kwargs["keep"], 3)

    def test_daily_monthly_pairs_and_other_project_untouched(self):
        _, first = self.backup()
        (self.repo / "tracked.txt").write_text("second", encoding="utf-8")
        _, second = self.backup()
        result = sg.compact_daily_snapshots(self.archives, Path("project"))
        self.assertEqual(result.moved_snapshots, 1)
        purged = self.archives / "$purge$" / first.relative_to(self.archives)
        self.assertTrue(purged.exists())
        self.assertTrue(sg.checksum_path(purged).exists())
        other = self.archives / "other" / second.parent.name / second.name
        other.parent.mkdir(parents=True)
        shutil.copy2(second, other)
        shutil.copy2(sg.checksum_path(second), sg.checksum_path(other))
        old = second.parent.parent / "2026-09-01" / second.name
        old.parent.mkdir()
        shutil.copy2(second, old)
        shutil.copy2(sg.checksum_path(second), sg.checksum_path(old))
        self.assertEqual(sg.compact_monthly_snapshots(self.archives, Path("project"), 0).moved_snapshots, 1)
        self.assertTrue(other.exists())
        sg.verify_archive(second)

    def test_corrupt_survivor_prevents_compaction(self):
        _, first = self.backup()
        (self.repo / "tracked.txt").write_text("second", encoding="utf-8")
        _, second = self.backup()
        with second.open("ab") as out:
            out.write(b"damage")
        with self.assertRaises(sg.ArchiveError):
            sg.compact_daily_snapshots(self.archives, Path("project"))
        self.assertTrue(first.exists())

    def test_purge_collision_preserves_all_snapshots(self):
        _, first = self.backup()
        (self.repo / "tracked.txt").write_text("second", encoding="utf-8")
        _, second = self.backup()
        collision = self.archives / "$purge$" / first.relative_to(self.archives)
        collision.parent.mkdir(parents=True)
        collision.write_bytes(b"do not overwrite")
        with self.assertRaises(sg.SnapGitError):
            sg.compact_daily_snapshots(self.archives, Path("project"))
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(collision.read_bytes(), b"do not overwrite")

    def test_pair_move_failure_rolls_back(self):
        _, first = self.backup()
        (self.repo / "tracked.txt").write_text("second", encoding="utf-8")
        self.backup()
        original_rename = Path.rename
        def fail_checksum_move(path, destination):
            if path == sg.checksum_path(first):
                raise OSError("injected move failure")
            return original_rename(path, destination)
        with patch.object(Path, "rename", fail_checksum_move):
            with self.assertRaises(sg.SnapGitError):
                sg.compact_daily_snapshots(self.archives, Path("project"))
        sg.verify_archive(first)

    def test_interrupted_pair_move_rolls_back_even_after_rename(self):
        _, archive = self.backup()
        original_rename = Path.rename
        def interrupt_after_zip_move(path, destination):
            result = original_rename(path, destination)
            if path == archive:
                raise KeyboardInterrupt
            return result
        with patch.object(Path, "rename", interrupt_after_zip_move):
            with self.assertRaises(KeyboardInterrupt):
                sg.move_snapshots_to_purge(self.archives, [archive])
        sg.verify_archive(archive)
        self.assertEqual(list((self.archives / "$purge$").rglob("*.zip*")), [])

    def test_console_interrupt_cleans_partial_backup_and_ignores_repeats(self):
        _, previous = self.backup()
        (self.repo / "tracked.txt").write_text("second", encoding="utf-8")
        original_verify = sg.verify_archive
        original_unlink = Path.unlink
        signals = [signal.SIGINT]
        if hasattr(signal, "SIGBREAK"):
            signals.append(signal.SIGBREAK)
        for signum in signals:
            def interrupt_readback(path, *args, **kwargs):
                if path.name.endswith(".partial"):
                    self.assertTrue(sg.checksum_path(path).exists())
                    signal.raise_signal(signum)
                return original_verify(path, *args, **kwargs)
            def repeat_interrupt_during_cleanup(path, *args, **kwargs):
                if path.name.endswith(".partial"):
                    for repeated_signal in signals:
                        signal.raise_signal(repeated_signal)
                return original_unlink(path, *args, **kwargs)
            error_output = io.StringIO()
            with self.subTest(signal=signum), contextlib.redirect_stderr(error_output), patch.object(sg, "verify_archive", side_effect=interrupt_readback), patch.object(Path, "unlink", repeat_interrupt_during_cleanup), patch.object(sg, "compact_daily_snapshots") as daily, patch.object(sg, "compact_monthly_snapshots") as monthly:
                self.assertEqual(sg.main(["backup", str(self.repo), "-sr", str(self.archives), "-cd", "-cm", "0"]), sg.EXIT_ERROR)
                daily.assert_not_called()
                monthly.assert_not_called()
            self.assertEqual(error_output.getvalue().strip(), "ERROR: Interrupted by user.")
            self.assertFalse((self.archives / "project" / ".snapgit.lock").exists())
            self.assertEqual(list(self.archives.rglob("*.partial*")), [])
            self.assertEqual(list(self.archives.rglob("*.zip")), [previous])
            self.assertEqual(list(self.archives.rglob("*.sha256")), [sg.checksum_path(previous)])
            sg.verify_archive(previous)

    def test_interrupted_restore_preserves_destination_and_cleans_temporary_files(self):
        (self.repo / "extra.txt").write_text("saved", encoding="utf-8")
        _, archive = self.backup()
        target = self.clone()
        destination = target / "extra.txt"
        destination.write_text("existing", encoding="utf-8")
        staging = []
        original_verify = sg.verify_archive
        def record_staging(path, extraction_root=None):
            staging.append(extraction_root)
            return original_verify(path, extraction_root)
        def interrupt_copy(source, output, length):
            output.write(source.read(1))
            signal.raise_signal(signal.SIGINT)
        with contextlib.redirect_stderr(io.StringIO()), patch.object(sg, "verify_archive", side_effect=record_staging), patch.object(shutil, "copyfileobj", side_effect=interrupt_copy):
            self.assertEqual(sg.main(["restore", str(target), "--snapshot", str(archive), "--overwrite"]), sg.EXIT_ERROR)
        self.assertEqual(destination.read_text(encoding="utf-8"), "existing")
        self.assertEqual(list(target.rglob(".snapgit-*")), [])
        self.assertEqual(len(staging), 1)
        self.assertFalse(staging[0].exists())

    def test_failed_publish_preserves_previous_backup(self):
        _, first = self.backup()
        (self.repo / "tracked.txt").write_text("second", encoding="utf-8")
        original_rename = Path.rename
        def fail_zip_publish(path, destination):
            if path.suffix == ".partial":
                raise OSError("injected publish failure")
            return original_rename(path, destination)
        with patch.object(Path, "rename", fail_zip_publish):
            with self.assertRaises(sg.ArchiveError):
                self.backup()
        sg.verify_archive(first)
        self.assertEqual(list(self.archives.rglob("*.zip")), [first])
        self.assertEqual(list(self.archives.rglob("*.sha256")), [sg.checksum_path(first)])

    def test_failure_skips_cleanup_and_releases_lock(self):
        args = ["backup", str(self.repo), "-sr", str(self.archives), "-cd", "-cm", "0"]
        with patch.object(sg, "write_archive", side_effect=sg.ArchiveError("write failure")), patch.object(sg, "compact_daily_snapshots") as daily:
            self.assertEqual(sg.main(args), 1)
            daily.assert_not_called()
        self.assertFalse((self.archives / "project" / ".snapgit.lock").exists())

    def test_legacy_directory_restore_rejected(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        with self.assertRaisesRegex(sg.SnapGitError, "ZIP snapshot"):
            sg.restore_extra_files(self.repo, legacy)

    def test_readback_failure_never_publishes(self):
        with patch.object(sg, "verify_archive", side_effect=sg.ArchiveError("injected damage")):
            with self.assertRaises(sg.ArchiveError):
                sg.create_backup(self.repo, self.archives)
        self.assertEqual(list(self.archives.rglob("*.zip")), [])
        self.assertEqual(list(self.archives.rglob("*.partial*")), [])

    def test_source_changes_during_write_never_publish(self):
        real_write = sg.write_archive
        def changing_write(path, manifest, root, lists, check):
            (root / "tracked.txt").write_text("racing writer", encoding="utf-8")
            return real_write(path, manifest, root, lists, check)
        with patch.object(sg, "write_archive", side_effect=changing_write):
            with self.assertRaises(sg.ArchiveError):
                sg.create_backup(self.repo, self.archives)
        self.assertEqual(list(self.archives.rglob("*.zip")), [])

    def test_cli_skip_and_lock(self):
        args = ["backup", str(self.repo), "-sr", str(self.archives), "-cd", "-cm", "0"]
        self.assertEqual(sg.main(args), 0)
        with patch.object(sg, "compact_daily_snapshots") as daily, patch.object(sg, "compact_monthly_snapshots") as monthly:
            self.assertEqual(sg.main(args), 0)
            daily.assert_not_called()
            monthly.assert_not_called()
        with sg.project_archive_lock(self.archives / "project"):
            self.assertEqual(sg.main(args), 1)

    @unittest.skipIf(os.name == "nt", "Unix filename and permission semantics")
    def test_linux_bytes_permissions_and_symlinks(self):
        name = os.fsdecode(b"raw-\xff")
        (self.repo / name).write_bytes(b"raw filename")
        (self.repo / "script").write_bytes(b"executable")
        (self.repo / "script").chmod(0o751)
        (self.repo / "link").symlink_to("script")
        _, archive = self.backup()
        target = self.clone()
        result = sg.restore_extra_files(target, archive)
        self.assertEqual(result.failed, 0)
        self.assertEqual((target / name).read_bytes(), b"raw filename")
        self.assertEqual((target / "script").stat().st_mode & 0o777, 0o751)
        self.assertTrue((target / "link").is_symlink())


class ConsoleInterruptTests(unittest.TestCase):
    def console_handlers(self):
        return {signum: signal.getsignal(signum)
                for name in ("SIGINT", "SIGBREAK")
                if (signum := getattr(signal, name, None)) is not None}

    def test_console_signals_interrupt_argument_parsing_and_restore_handlers(self):
        previous = self.console_handlers()
        for signum in previous:
            def interrupt_parsing(argv):
                signal.raise_signal(signum)
            error_output = io.StringIO()
            with self.subTest(signal=signum), contextlib.redirect_stderr(error_output), patch.object(sg, "parse_arguments", side_effect=interrupt_parsing):
                self.assertEqual(sg.main([]), sg.EXIT_ERROR)
            self.assertEqual(error_output.getvalue().strip(), "ERROR: Interrupted by user.")
            self.assertEqual(self.console_handlers(), previous)

    def test_handlers_are_restored_after_success_or_argparse_exit(self):
        previous = self.console_handlers()
        with contextlib.redirect_stdout(io.StringIO()), patch.object(sg, "verify_archive", return_value={"files": []}):
            self.assertEqual(sg.main(["verify", sg.__file__]), sg.EXIT_OK)
        self.assertEqual(self.console_handlers(), previous)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            sg.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(self.console_handlers(), previous)


class StatSignatureTests(unittest.TestCase):
    def test_windows_normalizes_only_synthetic_execute_bits(self):
        fields = dict(st_dev=1, st_ino=2, st_mode=stat.S_IFREG | 0o777,
                      st_size=42, st_mtime_ns=123, st_ctime_ns=456)
        before = SimpleNamespace(**fields)
        with patch.object(sg.os, "name", "nt"):
            expected = sg.stat_signature(before)
            opened = SimpleNamespace(**{**fields, "st_mode": stat.S_IFREG | 0o666})
            self.assertEqual(expected, sg.stat_signature(opened))
            for field, value in (("st_dev", 3), ("st_ino", 3), ("st_size", 43),
                                 ("st_mtime_ns", 124),
                                 ("st_mode", stat.S_IFREG | 0o555),
                                 ("st_mode", stat.S_IFLNK | 0o777)):
                with self.subTest(field=field, value=value):
                    changed = SimpleNamespace(**{**fields, field: value})
                    self.assertNotEqual(expected, sg.stat_signature(changed))

    def test_linux_execute_bits_and_ctime_remain_significant(self):
        fields = dict(st_dev=1, st_ino=2, st_mode=stat.S_IFREG | 0o777,
                      st_size=42, st_mtime_ns=123, st_ctime_ns=456)
        with patch.object(sg.os, "name", "posix"):
            expected = sg.stat_signature(SimpleNamespace(**fields))
            for changed in ({"st_mode": stat.S_IFREG | 0o666}, {"st_ctime_ns": 457}):
                self.assertNotEqual(expected, sg.stat_signature(SimpleNamespace(**{**fields, **changed})))


class KeepArgumentTests(unittest.TestCase):
    def test_version_without_subcommand_or_project(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            sg.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), "SnapGit 0.1.0")

    def test_default_aliases_and_large_integer(self):
        base = ["backup", ".", "-sr", "."]
        self.assertEqual(sg.parse_arguments(base).keep, 1)
        for option in ("--keep", "-k"):
            for count in (1, 3, 10**40):
                self.assertEqual(sg.parse_arguments([*base, option, str(count)]).keep, count)

    def test_invalid_counts_rejected_before_work(self):
        for value in ("0", "-1", "1.5", "abc", ""):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    sg.parse_arguments(["backup", ".", "-sr", ".", "--keep", value])
                self.assertEqual(caught.exception.code, 2)
        for count in (0, -1, 1.5, True):
            with self.assertRaises(sg.SnapGitError):
                sg.compact_daily_snapshots(Path("."), Path("project"), keep=count)
            with self.assertRaises(sg.SnapGitError):
                sg.compact_monthly_snapshots(Path("."), Path("project"), 0, keep=count)


if __name__ == "__main__":
    unittest.main()
