# Changelog

## Unreleased

- Handle Ctrl-C and Windows Ctrl-Break with cleanup and exit code 1; ignore
  repeated interrupts during cleanup and roll back interrupted compaction moves.

## 0.1.0 — 2026-09-28

Initial testing release. PyPI distribution files are prepared but are not published.

- Create standalone ZIP64 snapshots of Git working trees, including tracked,
  untracked and ignored files selected by `.snapignore`.
- Verify ZIP CRC, per-file SHA-256 and the whole archive before publishing a
  snapshot and before restoring it. Keep the companion `.zip.sha256` file.
- Skip backup and compaction when the verified latest snapshot matches the
  selected files and Git state.
- Restore local extras or the saved Git commit and working tree, including file
  permissions, symbolic links and intentional tracked-file deletions.
- Compact daily/monthly snapshots into `$purge$`, protecting the globally latest
  N snapshots with `--keep N` (default: 1).
- Support Windows and Linux, including non-UTF-8 Linux filenames.
- Avoid false change detection for Windows executable filename extensions.
- Install the `snapgit` command with `python -m pip install -e .`; expose
  `snapgit --version`. The implementation remains a single standalone Python file.

### Limitations

- This release is under testing. Archives use snapshot format 2; older directory
  snapshots are unsupported.
- Full restore records but does not recreate the Git staging boundary.
- Restore needs temporary disk space for the uncompressed snapshot.
- A live database or changing project needs a separate consistency strategy;
  byte-level integrity checks do not provide an atomic application snapshot.
