# SnapGit

**Verified ZIP snapshots of Git working trees.** SnapGit backs up tracked,
untracked and Git-ignored files, with independent `.snapignore` filtering.
Each snapshot is a complete, standalone archive.

Version **0.1.0** is a testing release. Packaging is prepared for PyPI, but no
PyPI publication is part of this release.

## Requirements

- Python 3.9 or newer and Git available on `PATH`.
- Windows or Linux. macOS has not been validated.
- No third-party Python runtime dependencies or external archiver.
- The source project must have a `.git` directory, `.gitignore` and `.snapignore`
  at its root. The two ignore files may be empty. Linked Git worktrees with a
  `.git` file are not supported.
- The snapshot destination must be outside the source project.

## Installation

Clone this repository, then install from the checkout:

```console
git clone https://github.com/Prevalex/SnapGit.git
cd SnapGit
python -m pip install -e .
snapgit --version
snapgit --help
```

Repository access is required to clone a private repository or download its
release assets. Editable installation uses this checkout directly: keep it in
place, and source changes take effect without reinstalling. Reinstall after
changing package metadata or command entry points.

For an isolated installation, create and activate a virtual environment first:

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
snapgit --version
```

**Linux**

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
snapgit --version
```

Pip installs a `snapgit` command into the selected Python environment's scripts
folder (`Scripts` on Windows, `bin` on Linux). Activating the environment puts
that folder on `PATH`. For an installation outside a virtual environment, ensure
the corresponding scripts folder is on `PATH`. The command then works from any
working directory on that computer. Installation is required on each computer.

If the command is not on `PATH`, use the same interpreter that installed it:

```console
python -m SnapGit --help
```

You can also install a locally built wheel without an editable checkout:

```console
python -m pip install dist/snapgit-0.1.0-py3-none-any.whl
```

The implementation remains a single file. Direct execution with
`python SnapGit.py ...` is supported without installing the package.

## Quick start

In the project to back up, create a `.snapignore` file. For example:

```gitignore
.git/
.venv*/
__pycache__/
.pytest_cache/
build/
dist/
*.pyc
*.tmp
```

Create a snapshot, using paths appropriate for your system:

```console
snapgit backup /path/to/project --snap-root /path/to/backups
```

On Windows, for example:

```powershell
snapgit backup "C:\Projects\MyProject" --snap-root "D:\Backups"
```

`PROJECT_ROOT` is required. Use `.` for the current directory. `--snap-root` has
alias `-sr`; alternatively, set `SNAP_ROOT`. Explicit options override environment
variables. SnapGit requires an existing repository and does not run Git network
operations automatically.

## Snapshot layout

```text
<snapshot-root>/<project>/YYYY-MM-DD/
    HH-mm-ss.zip
    HH-mm-ss.zip.sha256
```

To mirror the project hierarchy below a common source directory:

```console
snapgit backup /workspace/team/project --source-root /workspace --snap-root /backups
```

This creates snapshots below `/backups/team/project/`. `--source-root` overrides
`SNAP_SOURCE_ROOT`. Without either, only the project's directory name is used.
The project must be strictly inside the source root.

Inside the ZIP:

```text
snapshot.json
tracked.lst
untracked.lst
ignored.lst
backup-files.lst
backup/...                 project files with their relative paths
```

The manifest records the Git commit, branch, remote, selected staged/unstaged and
deleted paths, and each file's category, size, SHA-256, permissions, modification
time and symlink metadata. The `.lst` files are diagnostic; restore uses the
verified manifest. Git history itself is not included.

Archives use **Deflate level 9** with **ZIP64** for large files. Already compressed
formats (`xlsx`, `xlsm`, `docx`, `pptx`, `zip`, `7z`, `rar`, `gz`, `jpg`, `jpeg`,
`png`, `mp3`, `mp4`, `pdf`) are stored without recompression. Encryption and
Deflate64 are not used.

A ZIP containing a file larger than 4 GiB and Unicode filenames was successfully
tested with WinRAR 7.23 and PKWARE SecureZIP 14.50. Use WinRAR, rather than the
RAR-only `rar` command, for ZIP archives.

This release uses **snapshot format 2**. Legacy directory snapshots are not
supported. Version `0.1.0` is the application version, separate from the snapshot
format version.

## Integrity verification

**Keep each `.zip.sha256` file together with its ZIP.** SnapGit rejects a missing
or mismatched checksum. The ZIP can still be opened by ordinary ZIP tools.

During backup, SnapGit hashes the selected source files and writes data in
bounded chunks, without creating an uncompressed staging copy. It verifies the
source hashes again while writing, flushes the ZIP to disk, computes the archive
hash, then reopens and fully reads the archive. Archive SHA-256, member CRC,
member size/SHA-256 and manifest membership must all pass before publication.
It also checks for source file and Git-state changes during the operation.

Failed or interrupted operations do not publish a complete snapshot or start
compaction. A process or system crash may leave `.partial` files or an orphan
checksum; these are not treated as completed snapshots.

Ctrl-C, and Ctrl-Break on Windows, stop the command with `Interrupted by user`
and exit code `1`. Temporary files and the project lock are cleaned up; repeated
keypresses are ignored during cleanup. An interrupted compaction rolls back its
file moves. An interrupted restore may leave already applied changes in place;
it does not roll back the whole working tree.

Verify an existing snapshot without restoring or writing its contents to disk:

```console
snapgit verify /backups/project/2026-09-28/14-30-00.zip
```

Both restore modes, including `--dry-run`, verify the entire snapshot before
changing the target project. Regular destination files are replaced using a
verified temporary file.

Checksums detect corruption; they do not repair it or authenticate an archive's
author. ZIP archives have no recovery record. Maintain independent copies when
recovery from physical data loss is required.

## Unchanged projects

Before creating a snapshot, SnapGit verifies the latest ZIP for this project and
compares it with the current selected files and Git state. Comparison includes:

- File additions, removals, content hashes, permissions and symlink targets.
- Tracked/untracked/ignored categories.
- Git HEAD, branch, remote and selected staged/unstaged/deleted paths.

A timestamp-only change does not create another backup. Changes to excluded
files alone do not trigger a snapshot. A new Git commit does count as a change.

If nothing changed, SnapGit reports `No changes`, returns success and skips both
backup and compaction. A damaged latest snapshot cannot justify skipping a new
backup: SnapGit reports the problem and attempts to create a replacement.

This check reads the source files and the latest archive; it deliberately does
not rely only on sizes and timestamps.

## Retention and compaction

Compaction runs only after a new verified snapshot has been created. It affects
only the current project and moves ZIP/checksum pairs to
`<snapshot-root>/$purge$/<original-relative-path>` without deleting them.

```console
snapgit backup /path/to/project --snap-root /backups -cd -cm -1 --keep 3
```

- `--compact-day` / `-cd` keeps the latest snapshot of each day, plus protected
  snapshots.
- `--compact-month` / `-cm` keeps the latest snapshot of each eligible month,
  plus protected snapshots. `0` includes the current month, `-1` ends with the
  previous month, and `-2` ends with the month before that. Without a number,
  `-cm` uses `-6`. Positive offsets are rejected. Put the project argument before
  a bare `-cm` option.
- `--keep N` / `-k N` protects the **N globally latest snapshots of the project**
  from both operations. It is a minimum retained history, not a per-day or
  per-month count and not a maximum. The default is `1`; only integers of at
  least `1` are accepted. The newly created snapshot is included.

If the project has at most N snapshots, none are moved. In particular, a sole
snapshot is always protected. When both operations are requested, daily
compaction runs first; monthly compaction preserves the same latest N snapshots.
Snapshots after the monthly cutoff still count toward this global protection.
Other projects, `$purge$` and legacy snapshots are excluded from the count.

For example, with 10 snapshots in an old month and 3 in the current month,
`-cm -1 -k 3` leaves one from the old month and all 3 current snapshots: 4 total.
If all 10 snapshots are in a single eligible month, `-k 3` retains its latest 3.

Retained snapshots in affected periods are verified before older ones are moved.
A corrupt survivor or a destination conflict stops the operation. Existing
`$purge$` files are never overwritten.

A `.snapgit.lock` file prevents concurrent backup/compaction runs for the same
project. After a crash, remove a stale lock only after confirming that no process
is using that project's archive. Snapshot names have one-second resolution;
collisions fail rather than overwrite an existing snapshot.

## Restore

Restore local files after cloning or updating a repository:

```console
snapgit restore /path/to/project --snapshot /backups/project/2026-09-28/14-30-00.zip --dry-run
```

The default `extras` mode restores only files that were untracked or ignored in
the snapshot. It never overwrites files currently tracked by Git. Existing local
files are conflicts; `--overwrite` permits replacing regular local files, but not
tracked files, directories or destination symlinks. Extras mode deletes nothing
and does not require `.snapignore` in the target.

Remove `--dry-run` to apply the restore. To restore the saved Git commit and
working tree instead:

```console
snapgit restore /path/to/project --snapshot /backups/project/2026-09-28/14-30-00.zip --mode full --dry-run
```

Full restore requires the saved commit to be available locally and a clean target
working tree with no untracked files. Fetch missing commits yourself. After
verification and conflict checks, SnapGit checks out a detached HEAD, overlays
saved files and repeats intentional tracked-file deletions. Excluded deletions
are not applied. `--overwrite` permits replacing conflicting local files.

Full restore records but does not recreate the Git staging boundary: local
changes are restored to the working tree after checkout.

**Restore requires temporary disk space for the uncompressed snapshot**, even
with `--dry-run`. It first verifies and stages the contents in the system temporary
directory, then applies them. Replacing a destination file also needs room for
its temporary copy. Failure during initial staging leaves the target unchanged.
An error during application can result in a partial restore (exit code 2).

Symbolic links may require Developer Mode or additional privileges on Windows.
Linux names that cannot be represented safely on Windows are rejected. Non-UTF-8
Linux names are stored using technical ZIP member names and their original names
in the manifest; use SnapGit on Linux to restore those original names. Use SnapGit
for restoration when file permissions and symlinks matter.

## `.snapignore` rules

`.gitignore` controls Git; `.snapignore` independently controls backup selection.
A large ignored file is included unless `.snapignore` excludes it.

Supported rules are blank lines, `#` comments, directory patterns (`name/`),
root-relative patterns (`/name/`), `*` (except path separators), `**` (across
levels), `?` and re-inclusion with `!pattern`. Later rules take precedence.
Case sensitivity follows Git's `core.ignorecase` setting. This is a limited
subset of `.gitignore` syntax.

## Limits and exit codes

A multi-file backup is not an atomic filesystem snapshot. Stop applications that
modify the project when consistency across files matters. For a live SQLite
database, use SQLite's backup API to obtain a consistent copy, or stop the writer.
ZIP/hash checks do not verify database semantics or merge a WAL into its database.

SnapGit does not replace Git push/fetch, Git history storage or independent
backup copies. Uncommitted submodule contents and linked worktrees are outside
its supported repository model.

| Exit code | Meaning |
| --- | --- |
| `0` | Success, including no changes detected |
| `1` | Argument, Git, backup, verification or compaction error |
| `2` | Restore conflicts or partial file-application errors |

Run `snapgit backup --help`, `snapgit restore --help` or `snapgit verify --help`
for command-specific options.

## Development and local distribution builds

```console
python -m pip install -e .
python -m unittest -v test_snapgit
python -m pip install build twine
python -m build
python -m twine check dist/*
```

Builds produce a source distribution and a platform-independent wheel in `dist/`.
The wheel contains the single `SnapGit` module and its console entry point.
The source distribution additionally contains tests and release documentation;
local deployment helpers and internal workspace notes are excluded.

Package metadata and the CLI share the version in `SnapGit.__version__`.
See [CHANGELOG.md](CHANGELOG.md) for release notes. These commands build and check
artifacts locally; they do not upload anything to PyPI.

## License

SnapGit is released under the [MIT License](LICENSE).
