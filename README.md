# fix_torrent_paths.py

Checks, for every torrent in qBittorrent, whether its files sit in the
**category storage location configured in qBittorrent**. If not, the
script moves them there (locally on the filesystem) and then triggers a
recheck.

## How the move works

1. **Mismatch detection - two-stage:**
   - **API comparison:** the `save_path` reported by qBittorrent for the
     torrent is compared with the configured category storage location.
   - **Filesystem scan (enabled by default, disable with
     `--no-scan-filesystem`):** additionally, all known category folders
     are physically searched. If a torrent's content is sitting where
     ANOTHER category expects it, this is detected - even if the API's
     `save_path` already (incorrectly) looks correct. In practice this
     happens when qBittorrent doesn't cleanly complete/aborts an internal
     move but still updates `save_path`, even though the files were never
     actually moved.
2. **Local file merge:** the script itself moves the torrent content from
   its current folder (as determined via the API or the scan) into the
   correct category folder.
   - If a file with the same name already exists at the destination
     (e.g. because the torrent partly ended up there before), **the
     larger file wins**: the smaller one gets overwritten or deleted.
3. **AutoTMM off -> `setLocation` -> recheck -> AutoTMM on** (via the
   qBittorrent API), so qBittorrent's internal path management matches
   the actual location again.

## Torrents are paused during the run

By default (in live mode, **not** in `--dry-run`), the script pauses
**all** torrents in qBittorrent before the first file merge starts, and
only resumes them **after** all moves and rechecks have completed - even
if the script aborts or an error occurs partway through (`try/finally`),
so torrents never stay paused permanently. This avoids write
conflicts/race conditions during the file merges. Use
`--no-pause-all-during-run` to disable this, e.g. if seeding absolutely
needs to keep running during the run.

## Docker note (important!)

qBittorrent runs as a Docker container in your setup. The paths returned
by the API (e.g. `/data/torrents/...`) are **container paths**. If this
script runs on the host or in a different container, those are **not
automatically the same paths** as locally for the script.

That's why there's `--container-root` / `--local-root` (or the
environment variables `QBIT_CONTAINER_ROOT` / `QBIT_LOCAL_ROOT`):

```bash
--container-root /data/torrents \
--local-root /mnt/user/share_media/torrents
```

The script translates every path reported by the API into the path
locally reachable for the script, before it touches anything on disk.
**The `--local-root` path must be mounted readable AND writable for the
script** (e.g. the same bind mount/share used by the qBittorrent
container).

If nothing is given, the script assumes the API path and local path are
identical (e.g. because the script runs in the same mount namespace as
qBittorrent).

### `run.sh`: one script, asks docker or local

`run.sh` - it works for both setups (qBittorrent
running locally in the same mount namespace, or in Docker with different
mounts). It simply asks you interactively which one applies:

```
Does qBittorrent run in Docker (with different mounts than this script),
or locally/in the same mount namespace? (docker/local) [local]:
```

- Answer `docker` (or `d`): you'll then be asked for the container path
  and the local path, and `run.sh` passes them on as
  `--container-root`/`--local-root` to `fix_torrent_paths.py`.
- Answer `local` (or just press Enter, since that's the default): no
  path mapping is set at all, and `fix_torrent_paths.py` assumes the API
  paths are reachable 1:1 locally - the correct behavior when
  qBittorrent runs directly on the same system/mount namespace as the
  script.
- If `QBIT_CONTAINER_ROOT`/`QBIT_LOCAL_ROOT` are already set via an
  `.env` file or exported environment variables, the question is skipped
  entirely and that mapping is used as-is.

`fix_torrent_paths.py` itself doesn't need this question asked
separately - it already treats "no mapping given" as "local setup" by
design (see the log message it prints in that case). `run.sh` is just
the interactive convenience layer on top of that.

### Ignoring folders/sources completely (e.g. cross-seed)

If you want certain source folders never to be touched - e.g. because
they only contain cross-seed hardlinks you don't care about - use
`--skip-source-prefix` (repeatable) or the environment variable
`QBIT_SKIP_SOURCE_PREFIXES` (comma-separated):

```bash
python3 fix_torrent_paths.py \
    --container-root /data/torrents --local-root /mnt/user/share_media/torrents \
    --skip-source-prefix /data/cross-seed \
    --dry-run -v
```

Torrents whose current storage location (as reported by the API) starts
with this prefix are skipped entirely - no merge attempt, no warning, no
`setLocation`/recheck. They show up in the overview as "Ignored (skip
prefix)", not as "misplaced". This is the right approach if you
fundamentally never want to touch these sources (e.g. because they sit
outside your `--local-root` mount).

### Multiple mount roots (if you do want to manage them)

Some setups have more than one Docker mount root - e.g. `/data/torrents`
for regular downloads and additionally `/data/cross-seed` for cross-seed
hardlinks. `--container-root`/`--local-root` only cover **one** root. If
you don't want to ignore a second root but actually want it handled too
(files there moved as well), there's `--root-map` (repeatable) or
`QBIT_ROOT_MAP` (comma-separated), format
`container_path=local_path`:

```bash
python3 fix_torrent_paths.py \
    --container-root /data/torrents --local-root /mnt/user/share_media/torrents \
    --root-map /data/cross-seed=/mnt/user/share_media/cross-seed \
    --dry-run -v
```

`--container-root`/`--local-root` are still supported and are simply
added as an extra mapping. The most specific (longest) mapping wins if
paths overlap.

### Orphaned/misplaced data outside the category structure (e.g. `orphaned_data`)

Sometimes torrent data ends up in a catch-all folder that itself doesn't
correspond to any category (e.g. `orphaned_data`), for instance because
an automated tool "parked" it there. With `--extra-scan-dir` (repeatable)
or `QBIT_EXTRA_SCAN_DIRS` (comma-separated), the script can additionally
search such folders:

```bash
python3 fix_torrent_paths.py \
    --container-root /data/torrents --local-root /mnt/user/share_media/torrents \
    --extra-scan-dir /mnt/user/share_media/torrents/orphaned_data \
    --dry-run -v
```

Important: **`--extra-scan-dir` expects an already-local path**, not a
container path - no `--root-map`/`--container-root` translation is
applied to it, since this folder isn't a category of its own.

Anything found at the top level there that can be matched by name to a
known qBittorrent torrent is **always** treated as misplaced (regardless
of the API `save_path`) and moved into the folder belonging to that
torrent's category - on a name conflict, the larger file wins as usual,
followed by the normal `setLocation`+recheck flow. Entries that don't
match any known torrent are left untouched.

## Why AutoTMM is temporarily disabled

The following pattern was found in the qBittorrent log:
```
Torrent move canceled. ... Destination: "/data/torrents/sonarr-cartoon"
Failed to enqueue torrent move. ... Source/Destination: "/data/torrents". Reason: both paths point to the same location
```
qBittorrent apparently sometimes computes the AutoTMM destination path
twice and in doing so cancels the actually-correct move. Since the
script now moves the files itself physically, it completely sidesteps
this problem - `setLocation` afterwards only serves to update
qBittorrent's own path metadata.

## Installation

```bash
pip install requests --break-system-packages
```

(No other dependencies, no qbit_manage needed.)

## Usage

**Always run as a dry run first (nothing gets changed):**
```bash
python3 fix_torrent_paths.py \
    --host 192.168.178.22:8082 --user admin --password 'YOUR_PASSWORD' \
    --container-root /data/torrents --local-root /mnt/user/share_media/torrents \
    --dry-run -v
```

If the output looks right, run for real (same call without `--dry-run`):
```bash
python3 fix_torrent_paths.py \
    --host 192.168.178.22:8082 --user admin --password 'YOUR_PASSWORD' \
    --container-root /data/torrents --local-root /mnt/user/share_media/torrents
```

**Alternatively via environment variables** (keeps the password out of
shell history):
```bash
export QBIT_HOST=192.168.178.22:8082
export QBIT_USER=admin
export QBIT_PASS='YOUR_PASSWORD'
export QBIT_CONTAINER_ROOT=/data/torrents
export QBIT_LOCAL_ROOT=/mnt/user/share_media/torrents
python3 fix_torrent_paths.py --dry-run -v
```

### Useful options

| Option | Purpose |
|---|---|
| `--dry-run` | Only show what would happen, change nothing |
| `--categories sonarr-cartoon,radarr` | Only process specific categories |
| `--exclude-categories cross-seed` | Never touch specific categories |
| `--skip-source-prefix /data/cross-seed` | Never touch torrents whose current storage location is under this prefix (regardless of category) |
| `--no-scan-filesystem` | Disable the physical filesystem scan of category folders (only API `save_path` comparison) |
| `--extra-scan-dir /path/orphaned_data` | Search an additional local folder (not a container path) for known torrent contents and move matches into the right category folder |
| `--no-pause-all-during-run` | Do NOT pause ALL torrents while moving/rechecking (default: all torrents are paused first and guaranteed to be resumed afterwards) |
| `--root-map /data/x=/local/x` | Additional container->local path mapping (repeatable) |
| `--no-recheck` | Don't trigger a recheck after the move |
| `--no-reenable-autotmm` | Leave AutoTMM disabled after the move |
| `--batch-size 25` | Number of torrents per API batch (default 25) |
| `--poll-timeout 600` | Max. wait time in seconds per move batch |
| `-v` | More verbose console output |

A log is always additionally written to `fix_torrent_paths.log`
(changeable via `--log-file`).

## Recommended workflow for your setup

1. First run `--dry-run -v` against **a single affected category**, e.g.:
   ```bash
   python3 fix_torrent_paths.py \
       --host 192.168.178.22:8082 --user admin --password 'xxx' \
       --container-root /data/torrents --local-root /mnt/user/share_media/torrents \
       --categories short-seed --dry-run -v
   ```
2. Check the result (especially the "overview of required moves" and the
   logged conflicts/size comparisons), then run without `--dry-run` for
   that one category.
3. Verify in qBittorrent (storage location, status after recheck).
4. Once everything looks right, repeat for the remaining categories or
   drop the `--categories` filter to process all of them at once.
5. If you're still using qbit_manage: set `cat_update_all: false` there,
   so the problem doesn't get triggered again by every qbit_manage run
   (see the earlier analysis).

## Safety notes (additional)

- The file merge happens **locally via Python** (`shutil.move` /
  `os.remove`), not via the qBittorrent API. Make sure `--local-root`
  correctly points to the same storage volume as the qBittorrent
  container - a wrong path leads at best to "file not found" warnings,
  at worst to actions in the wrong place. **That's why you should always
  use `--dry-run` first.**
- On a name conflict, the **larger** file is always kept, regardless of
  whether it currently sits at the source or already at the destination.
  This is usually correct (smaller file = incomplete/failed download),
  but it's no substitute for a checksum.

## Safety notes

- The script **only** touches torrents whose `save_path` doesn't match
  the category storage location configured in qBittorrent. Torrents
  without a category, or with categories that have no storage location
  set, are skipped (not touched).
- Torrents whose move doesn't complete within `--poll-timeout` are
  **not** rechecked and AutoTMM is **not** re-enabled for them - they are
  marked "Timeout" in the log and should be checked manually.
- Recommendation: before the first live run, plan for a backup/snapshot
  of the torrent data, or enough buffer in a recycle bin/backup.
