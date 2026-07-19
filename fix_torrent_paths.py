#!/usr/bin/env python3
"""
fix_torrent_paths.py

For every torrent in qBittorrent, checks whether its files live in the
correct category subfolder (as configured in qBittorrent itself under
"Categories -> Save path"). If not:

  1. The files are moved locally on disk from the wrong to the correct
     category folder (done in Python, not via the qBittorrent API - see
     the Docker note below).
     - If a file with the same name already exists at the destination,
       the LARGER file wins: the smaller one is overwritten/discarded.
  2. Automatic Torrent Management (AutoTMM) is temporarily disabled for
     the torrent (prevents the internal qBittorrent race condition that
     would otherwise immediately cancel an API move again - see
     qbittorrent.log: "Torrent move canceled" followed by
     "Failed to enqueue torrent move ... both paths point to the same
     location").
  3. setLocation is called so qBittorrent updates its internal path
     bookkeeping to the new (now already correct) location.
  4. The torrent is rechecked.
  5. AutoTMM is optionally re-enabled (once everything has safely
     finished).

IMPORTANT - qBittorrent runs as a Docker container:
  The paths reported by the qBittorrent API (e.g. "/data/torrents/...")
  are the paths INSIDE the container. If this script runs on the host
  (or in a different container), those are NOT the same paths as on the
  local filesystem. That's why there are the --container-root and
  --local-root options: they translate the path prefix reported by
  qBittorrent into the path actually reachable locally, before the
  script touches any files itself.

  Example (matching a setup like your qbit_manage mapping):
      --container-root /data/torrents
      --local-root /mnt/user/share_media/torrents

  MULTIPLE mappings: some setups have more than one Docker mount root
  (e.g. /data/torrents for regular downloads AND /data/cross-seed for
  cross-seed hardlinks). For that there's --root-map, repeatable or
  comma-separated, format container_path=local_path:
      --root-map /data/torrents=/mnt/user/share_media/torrents \\
      --root-map /data/cross-seed=/mnt/user/share_media/cross-seed
  or as a single environment variable QBIT_ROOT_MAP (comma-separated):
      QBIT_ROOT_MAP="/data/torrents=/mnt/.../torrents,/data/cross-seed=/mnt/.../cross-seed"
  --container-root/--local-root are still supported and are simply added
  as an extra mapping.

  If no mapping at all is given, the script assumes the paths reported
  by the API are reachable locally 1:1 (e.g. because the script itself
  runs in the same container/mount namespace as qBittorrent).

The script talks to the qBittorrent WebAPI for all torrent control
commands, but touches the local filesystem directly for the actual file
merge (for that, --local-root must be mounted readable AND writable for
this script).

Usage:
    python3 fix_torrent_paths.py --host 192.168.178.22:8082 --user admin --password 'xxx' \\
        --container-root /data/torrents --local-root /mnt/user/share_media/torrents --dry-run

    python3 fix_torrent_paths.py --host 192.168.178.22:8082 --user admin --password 'xxx' \\
        --container-root /data/torrents --local-root /mnt/user/share_media/torrents

Alternatively, host/user/password/paths can also be set via environment
variables (handy so the password doesn't end up in shell history):
    export QBIT_HOST=192.168.178.22:8082
    export QBIT_USER=admin
    export QBIT_PASS='xxx'
    export QBIT_CONTAINER_ROOT=/data/torrents
    export QBIT_LOCAL_ROOT=/mnt/user/share_media/torrents
    python3 fix_torrent_paths.py --dry-run

Recommendation: ALWAYS run with --dry-run first and check the output
before moving anything for real.
"""

import argparse
import os
import shutil
import sys
import time
import logging
from datetime import datetime

try:
    import requests
except ImportError:
    print("The 'requests' package is missing. Install it with: pip install requests --break-system-packages")
    sys.exit(1)


# --------------------------------------------------------------------------
# Configuration / CLI
# --------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Finds torrents whose files are not in the configured "
                     "category subfolder, moves them there and triggers a "
                     "recheck."
    )
    p.add_argument("--host", help="qBittorrent WebUI host:port, e.g. 192.168.178.22:8082 "
                                   "(alternative: environment variable QBIT_HOST)")
    p.add_argument("--user", help="qBittorrent WebUI username (alternative: QBIT_USER)")
    p.add_argument("--password", dest="password", help="qBittorrent WebUI password (alternative: QBIT_PASS)")
    p.add_argument("--https", action="store_true", help="Use https instead of http for the WebUI")

    p.add_argument("--dry-run", action="store_true",
                    help="Only show what would be done, don't actually change anything")
    p.add_argument("--categories", help="Comma-separated list of categories to restrict to "
                                         "(default: all categories with a save path set)")
    p.add_argument("--exclude-categories", default="",
                    help="Comma-separated list of categories to NEVER touch")
    p.add_argument("--skip-source-prefix", action="append", default=None,
                    help="Torrents whose CURRENT save_path (from the API, container path) starts "
                         "with this prefix are skipped entirely (no merge attempt, no warning) - "
                         "e.g. --skip-source-prefix /data/cross-seed to never touch cross-seed "
                         "torrents. Can be given multiple times, alternatively/comma-separated: "
                         "environment variable QBIT_SKIP_SOURCE_PREFIXES.")

    p.add_argument("--no-recheck", action="store_true", help="Don't trigger a recheck after the move")
    p.add_argument("--no-reenable-autotmm", action="store_true",
                    help="Do NOT re-enable AutoTMM after the move (stays 'manual')")
    p.add_argument("--no-pause-all-during-run", action="store_true",
                    help="Don't pause ALL torrents in qBittorrent while the script moves files/"
                         "rechecks. By default (in live mode, not in --dry-run) ALL torrents are "
                         "paused before the first merge and restarted after all moves+rechecks "
                         "are complete - even on error or abort (try/finally) - to avoid write "
                         "conflicts during the file merges.")
    p.add_argument("--no-scan-filesystem", action="store_true",
                    help="Disables the additional filesystem scan of the category folders. "
                         "ACTIVE by default: physically checks whether torrent contents are sitting "
                         "in a different category's folder than the one the torrent is assigned to "
                         "in qBittorrent - even if the save_path reported by the API already looks "
                         "(incorrectly) correct, e.g. because an earlier move by qBittorrent itself "
                         "wasn't completed cleanly.")
    p.add_argument("--extra-scan-dir", action="append", default=None,
                    help="Additional LOCAL folder (not a container path, reachable directly as on "
                         "disk) that is not itself a valid category, but should be scanned for "
                         "torrent contents, e.g. "
                         "--extra-scan-dir /srv/mergerfs/config/share_media/torrents/orphaned_data. "
                         "Anything inside it whose name matches a known torrent is ALWAYS treated "
                         "as misplaced and moved into the matching category folder (larger file "
                         "wins on conflict) and then rechecked. "
                         "Can be given multiple times, alternatively/comma-separated: "
                         "environment variable QBIT_EXTRA_SCAN_DIRS.")

    p.add_argument("--poll-interval", type=float, default=5.0, help="Seconds between move status checks")
    p.add_argument("--poll-timeout", type=float, default=600.0, help="Max. seconds to wait for a move")

    p.add_argument("--batch-size", type=int, default=25,
                    help="Max. number of torrents per API call/batch (large categories are split up)")

    p.add_argument("--container-root", default=None,
                    help="Path prefix as reported by qBittorrent (inside the Docker container), "
                         "e.g. /data/torrents (alternative: QBIT_CONTAINER_ROOT). "
                         "Used as one mapping in addition to --root-map.")
    p.add_argument("--local-root", default=None,
                    help="The same location as it is reachable locally for THIS script, "
                         "e.g. /mnt/user/share_media/torrents (alternative: QBIT_LOCAL_ROOT). "
                         "If not set, the paths reported by the API are used 1:1 as local paths.")
    p.add_argument("--root-map", action="append", default=None,
                    help="Additional container->local path mapping in the format "
                         "container_path=local_path, e.g. /data/cross-seed=/mnt/user/share_media/cross-seed. "
                         "Can be given multiple times (for multiple Docker mount roots). "
                         "Alternative/comma-separated: environment variable QBIT_ROOT_MAP.")

    p.add_argument("--log-file", default="fix_torrent_paths.log", help="Path to the log file")
    p.add_argument("-v", "--verbose", action="store_true", help="More verbose console output")

    return p.parse_args()


def _clean(value):
    """
    Cleans a value coming from CLI/ENV/.env of typical troublemakers:
    leading/trailing whitespace, Windows line endings (\\r), and
    surrounding single/double quotes (in case the .env file wrapped the
    values in quotes).
    """
    if value is None:
        return value
    v = value.strip().strip("\r").strip("\n")
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        v = v[1:-1].strip()
    return v


def resolve_credentials(args):
    """CLI arguments take precedence, otherwise environment variables QBIT_HOST/QBIT_USER/QBIT_PASS."""
    host = _clean(args.host or os.environ.get("QBIT_HOST"))
    user = _clean(args.user or os.environ.get("QBIT_USER"))
    password = _clean(args.password or os.environ.get("QBIT_PASS"))

    if not host:
        print("No host given. Either use --host or set the environment variable QBIT_HOST.")
        sys.exit(1)

    # In case a scheme or trailing slash was accidentally included
    host = host.replace("http://", "").replace("https://", "").rstrip("/")

    return host, user, password


def resolve_path_mapping(args):
    """
    Builds the list of all container->local path mappings.
    Sources (all are combined):
      - --container-root/--local-root (or QBIT_CONTAINER_ROOT/QBIT_LOCAL_ROOT): one pair
      - --root-map (repeatable) or QBIT_ROOT_MAP (comma-separated): container=local
    Returns a list of (container_root, local_root) tuples, sorted by
    length of container_root descending (longest/most specific path first).
    """
    mappings = []

    container_root = _clean(args.container_root or os.environ.get("QBIT_CONTAINER_ROOT"))
    local_root = _clean(args.local_root or os.environ.get("QBIT_LOCAL_ROOT"))
    if container_root and local_root:
        mappings.append((norm(container_root), local_root.rstrip("/")))

    raw_entries = list(args.root_map or [])
    env_map = os.environ.get("QBIT_ROOT_MAP")
    if env_map:
        raw_entries.extend(env_map.split(","))

    for entry in raw_entries:
        entry = _clean(entry)
        if not entry:
            continue
        if "=" not in entry:
            print(f"Invalid --root-map/QBIT_ROOT_MAP format (expected container=local): {entry!r}")
            sys.exit(1)
        c, l = entry.split("=", 1)
        c, l = _clean(c), _clean(l)
        if not c or not l:
            print(f"Invalid --root-map/QBIT_ROOT_MAP format (expected container=local): {entry!r}")
            sys.exit(1)
        mappings.append((norm(c), l.rstrip("/")))

    # Check longest (most specific) container paths first, so e.g.
    # /data/cross-seed isn't accidentally swallowed by a more generic
    # /data mapping.
    mappings.sort(key=lambda pair: len(pair[0]), reverse=True)
    return mappings


def resolve_skip_prefixes(args):
    """
    List of normalized path prefixes (container paths) for which torrents
    should be skipped entirely, regardless of which category they are
    assigned to - e.g. cross-seed folders that should never be touched.
    """
    raw = list(args.skip_source_prefix or [])
    env_val = os.environ.get("QBIT_SKIP_SOURCE_PREFIXES")
    if env_val:
        raw.extend(env_val.split(","))
    prefixes = [norm(_clean(p)) for p in raw if _clean(p)]
    return prefixes


def matches_skip_prefix(save_path, skip_prefixes):
    p = norm(save_path)
    for prefix in skip_prefixes:
        if p == prefix or p.startswith(prefix + os.sep):
            return True
    return False


def resolve_extra_scan_dirs(args):
    """
    List of additional LOCAL folders (already reachable as on disk, no
    container-path translation) to be scanned for torrent contents even
    though they are not themselves a valid category (e.g. orphaned_data).
    Returns a dict label -> local_path, label = folder name (for log
    output).
    """
    raw = list(args.extra_scan_dir or [])
    env_val = os.environ.get("QBIT_EXTRA_SCAN_DIRS")
    if env_val:
        raw.extend(env_val.split(","))

    result = {}
    for entry in raw:
        entry = _clean(entry)
        if not entry:
            continue
        local_dir = entry.rstrip("/")
        label = os.path.basename(local_dir) or local_dir
        result[label] = local_dir
    return result


# --------------------------------------------------------------------------
# qBittorrent WebAPI client (minimal, only the endpoints we need)
# --------------------------------------------------------------------------

class QbitClient:
    def __init__(self, host, user, password, use_https=False, timeout=30):
        scheme = "https" if use_https else "http"
        self.base_url = f"{scheme}://{host}"
        self.session = requests.Session()
        self.timeout = timeout
        self._login(user, password)

    def _login(self, user, password):
        url = f"{self.base_url}/api/v2/auth/login"
        resp = self.session.post(
            url,
            data={"username": user or "", "password": password or ""},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        if resp.text.strip() != "Ok.":
            raise RuntimeError(
                f"Login to qBittorrent failed (response: {resp.text!r}). "
                f"Check host/user/password, or whether the WebUI auth whitelist "
                f"lets the source IP through without login (then 'Ok.' should be returned)."
            )

    def _get(self, path, params=None):
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def _post(self, path, data=None):
        url = f"{self.base_url}{path}"
        resp = self.session.post(url, data=data, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def get_categories(self):
        """-> dict: {category_name: {'name':..., 'savePath':...}}"""
        return self._get("/api/v2/torrents/categories").json()

    def get_torrents(self):
        """-> list of torrent dicts (hash, name, category, save_path, state, auto_tmm, ...)"""
        return self._get("/api/v2/torrents/info").json()

    def set_auto_management(self, hashes, enable):
        self._post("/api/v2/torrents/setAutoManagement", {
            "hashes": "|".join(hashes),
            "enable": "true" if enable else "false",
        })

    def pause_all(self):
        """
        Pauses ALL torrents in qBittorrent (hashes=all).
        Older qBittorrent WebUI API versions use the endpoint
        '/torrents/pause' for this, newer ones (from WebUI API 2.11,
        qBittorrent >= 4.6) renamed it to '/torrents/stop'. We try the
        old name first and fall back to the new one on 404.
        """
        try:
            self._post("/api/v2/torrents/pause", {"hashes": "all"})
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                self._post("/api/v2/torrents/stop", {"hashes": "all"})
            else:
                raise

    def resume_all(self):
        """Resumes ALL torrents in qBittorrent again (hashes=all), see pause_all()."""
        try:
            self._post("/api/v2/torrents/resume", {"hashes": "all"})
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                self._post("/api/v2/torrents/start", {"hashes": "all"})
            else:
                raise

    def set_location(self, hashes, location):
        self._post("/api/v2/torrents/setLocation", {
            "hashes": "|".join(hashes),
            "location": location,
        })

    def recheck(self, hashes):
        self._post("/api/v2/torrents/recheck", {
            "hashes": "|".join(hashes),
        })

    def get_torrents_by_hashes(self, hashes):
        info = self.get_torrents()
        wanted = set(hashes)
        return [t for t in info if t["hash"] in wanted]


# --------------------------------------------------------------------------
# Helper functions
# --------------------------------------------------------------------------

def norm(path):
    if not path:
        return ""
    return os.path.normpath(path).rstrip("/")


def build_reverse_category_lookup(categories):
    """
    Builds a mapping normalized path -> category name, so we can tell
    which (wrong) category a torrent is currently physically sitting in -
    not just THAT the path is wrong.
    """
    lookup = {}
    for name, info in categories.items():
        sp = norm(info.get("savePath"))
        if sp:
            lookup[sp] = name
    return lookup


def guess_current_category(actual_path, reverse_lookup):
    """
    Maps an actual save_path to a known category if it matches its save
    path exactly (or as a prefix). Returns None if the path doesn't match
    any known category (e.g. because the file sits directly in the root
    directory).
    """
    actual = norm(actual_path)
    if actual in reverse_lookup:
        return reverse_lookup[actual]
    # Fallback: prefix match, in case the torrent folder itself is still
    # nested under the category path (e.g. nested subfolders)
    for path, name in reverse_lookup.items():
        if path and (actual == path or actual.startswith(path + os.sep)):
            return name
    return None


def build_content_name_index(torrents):
    """
    Builds an index file name/folder name -> list of torrents, so we can
    map a folder/file physically found on disk back to a torrent in
    qBittorrent (name match). Uses both the torrent name and the basename
    of content_path, since either can be the actual top-level folder name
    on disk depending on the torrent's structure.
    """
    index = {}
    for t in torrents:
        keys = set()
        name = t.get("name")
        if name:
            keys.add(name)
        content_path = t.get("content_path")
        if content_path:
            keys.add(os.path.basename(content_path.rstrip("/\\")))
        for k in keys:
            if not k:
                continue
            index.setdefault(k, []).append(t)
    return index


def build_category_local_dirs(categories, mappings):
    """Category -> locally reachable folder (savePath translated via mappings)."""
    result = {}
    for cat_name, info in categories.items():
        sp = norm(info.get("savePath"))
        if not sp:
            continue
        result[cat_name] = translate_path(sp, mappings)
    return result


def scan_physical_mismatches(category_local_dirs, extra_scan_dirs, content_index, skip_prefixes,
                              logger, scan_category_dirs=True):
    """
    Physically scans on disk:
      1) EVERY known category folder (top level = torrent root folders/
         files) and reports a mismatch if a found torrent content
         actually belongs to a DIFFERENT category.
         (only if scan_category_dirs=True)
      2) Additional "extra folders" not tied to any category (e.g.
         orphaned_data) - anything found there that can be matched to a
         known torrent ALWAYS counts as a mismatch (since these folders
         are themselves never a valid torrent destination) and is moved
         into the folder belonging to the torrent's category.
    category_local_dirs is ALWAYS needed to look up the correct
    destination folder for a found torrent - even if scan_category_dirs
    is False (then only the category_local_dirs themselves are NOT
    scanned, but they're still used as the destination lookup table).
    In both cases, independent of whatever the API reports as save_path
    for the torrent (that field isn't always updated reliably by
    qBittorrent if a move fails/is aborted).

    Returns: dict cat_name -> list of torrent dicts (copies, with an
    additional key '_physical_src' = the actual local path found and
    '_physical_found_in_category' = label of the folder it was found in).
    """
    found = {}
    seen_hashes = set()

    def process_dir(local_dir, dir_label, dir_is_category):
        if not local_dir or not os.path.isdir(local_dir):
            if local_dir:
                logger.warning(f"Scan folder doesn't exist (skipped): {local_dir}")
            return
        try:
            entries = os.listdir(local_dir)
        except OSError as e:
            logger.warning(f"Could not read folder: {local_dir} ({e})")
            return

        for entry in entries:
            full_path = os.path.join(local_dir, entry)
            candidates = content_index.get(entry)
            if not candidates:
                continue  # no known torrent with this name - leave it alone

            for t in candidates:
                t_cat = t.get("category") or ""
                if not t_cat or t_cat not in category_local_dirs:
                    continue

                expected_local_dir = category_local_dirs[t_cat]
                if dir_is_category and norm(expected_local_dir) == norm(local_dir):
                    continue  # already sitting in the correct physical folder

                if skip_prefixes and matches_skip_prefix(t.get("save_path"), skip_prefixes):
                    continue

                if t["hash"] in seen_hashes:
                    continue
                seen_hashes.add(t["hash"])

                t_copy = dict(t)
                t_copy["_physical_src"] = full_path
                t_copy["_physical_found_in_category"] = dir_label
                found.setdefault(t_cat, []).append(t_copy)

    if scan_category_dirs:
        for cat_name, local_dir in category_local_dirs.items():
            process_dir(local_dir, cat_name, dir_is_category=True)

    for label, local_dir in extra_scan_dirs.items():
        process_dir(local_dir, label, dir_is_category=False)

    return found


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def translate_path(container_path, mappings):
    """
    Translates a path reported by qBittorrent (inside the Docker
    container) into the path reachable locally for THIS script, using the
    list of (container_root, local_root) mappings (see
    resolve_path_mapping - already sorted by prefix length descending,
    i.e. the most specific mapping wins). If no mapping is set or none
    matches, the path is returned unchanged (assumption: the script runs
    in the same mount namespace as qBittorrent).
    """
    if not container_path:
        return container_path
    if not mappings:
        return container_path

    p = norm(container_path)
    for c_root, local_root in mappings:
        if p == c_root or p.startswith(c_root + os.sep):
            rel = p[len(c_root):].lstrip(os.sep)
            return os.path.join(local_root, rel) if rel else local_root
    # Path is outside all mapped areas - return unchanged, but the caller
    # should probably treat this as a warning.
    return container_path


def human_size(num_bytes):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f}PB"


def merge_file(src_file, dst_file, logger, dry_run):
    """
    Moves a single file from src_file to dst_file.
    If dst_file already exists, the LARGER file wins:
      - source larger  -> destination gets overwritten
      - destination larger/equal -> source gets discarded (deleted)
    Returns True if the file ends up at dst_file afterwards (moved, or
    was already correctly there), False on error.
    """
    try:
        if os.path.exists(dst_file):
            src_size = os.path.getsize(src_file)
            dst_size = os.path.getsize(dst_file)
            if src_size > dst_size:
                logger.info(f"        Conflict: {os.path.basename(dst_file)} - "
                            f"source is larger ({human_size(src_size)} > {human_size(dst_size)}) "
                            f"-> destination gets overwritten")
                if not dry_run:
                    os.remove(dst_file)
                    shutil.move(src_file, dst_file)
            else:
                logger.info(f"        Conflict: {os.path.basename(dst_file)} - "
                            f"destination is larger/equal ({human_size(dst_size)} >= {human_size(src_size)}) "
                            f"-> source gets discarded")
                if not dry_run:
                    os.remove(src_file)
        else:
            logger.debug(f"        Moving: {src_file} -> {dst_file}")
            if not dry_run:
                os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                shutil.move(src_file, dst_file)
        return True
    except OSError as e:
        logger.error(f"        Error with {src_file} -> {dst_file}: {e}")
        return False


def cleanup_empty_dirs(root, logger, dry_run):
    """Removes empty directories below (and including) root after the merge."""
    if dry_run or not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                logger.debug(f"        Removed empty directory: {dirpath}")
        except OSError:
            pass


def merge_move(local_src, local_dst, logger, dry_run):
    """
    Moves the complete torrent content (file or folder) from local_src to
    local_dst, resolving name conflicts via "larger file wins". Returns
    (ok: bool, files_moved: int).
    """
    if norm(local_src) == norm(local_dst):
        return True, 0

    if not os.path.exists(local_src):
        logger.warning(f"        Source no longer exists locally: {local_src} - skipping file merge")
        return os.path.exists(local_dst), 0

    ok_all = True
    moved = 0

    if os.path.isfile(local_src):
        if not dry_run:
            os.makedirs(os.path.dirname(local_dst), exist_ok=True)
        ok = merge_file(local_src, local_dst, logger, dry_run)
        ok_all = ok_all and ok
        moved += 1 if ok else 0
    else:
        for dirpath, dirnames, filenames in os.walk(local_src):
            rel = os.path.relpath(dirpath, local_src)
            for fname in filenames:
                src_file = os.path.join(dirpath, fname)
                dst_file = os.path.join(local_dst, rel, fname) if rel != "." else os.path.join(local_dst, fname)
                ok = merge_file(src_file, dst_file, logger, dry_run)
                ok_all = ok_all and ok
                moved += 1 if ok else 0
        cleanup_empty_dirs(local_src, logger, dry_run)

    return ok_all, moved


def setup_logging(log_file, verbose):
    logger = logging.getLogger("fix_torrent_paths")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


# --------------------------------------------------------------------------
# Main logic
# --------------------------------------------------------------------------

def main():
    args = parse_args()
    logger = setup_logging(args.log_file, args.verbose)

    # --- Determine credentials ---
    host, user, password = resolve_credentials(args)
    mappings = resolve_path_mapping(args)
    skip_prefixes = resolve_skip_prefixes(args)
    extra_scan_dirs = resolve_extra_scan_dirs(args)
    logger.debug(f"Cleaned host: {host!r}  User: {user!r}")

    if skip_prefixes:
        logger.info(f"Ignoring torrents whose current save path is under: {', '.join(skip_prefixes)}")

    if extra_scan_dirs:
        logger.info(f"Additional scan folders (will be scanned for known torrents): "
                    f"{', '.join(extra_scan_dirs.values())}")

    if not mappings:
        logger.info("No --container-root/--local-root or --root-map set: it is assumed "
                     "that the paths reported by qBittorrent are reachable locally for this "
                     "script 1:1. This is the correct/expected case if qBittorrent runs "
                     "locally in the same mount namespace as this script (no Docker mapping "
                     "needed). If qBittorrent instead runs in Docker with DIFFERENT mounts "
                     "than this script, --container-root/--local-root or --root-map MUST be "
                     "set, otherwise wrong (non-existent) paths will be touched!")
    else:
        for c_root, l_root in mappings:
            logger.debug(f"Path mapping: {c_root}  ->  {l_root}")

    only_categories = None
    if args.categories:
        only_categories = {c.strip() for c in args.categories.split(",") if c.strip()}
    exclude_categories = {c.strip() for c in args.exclude_categories.split(",") if c.strip()}

    def category_allowed(cat_name):
        if only_categories is not None and cat_name not in only_categories:
            return False
        if cat_name in exclude_categories:
            return False
        return True

    logger.info("=" * 70)
    logger.info(f"Start {'(DRY-RUN)' if args.dry_run else ''} - Host: {host}")
    logger.info("=" * 70)

    client = QbitClient(host, user, password, use_https=args.https)

    categories = client.get_categories()
    torrents = client.get_torrents()
    reverse_lookup = build_reverse_category_lookup(categories)

    logger.info(f"Found {len(torrents)} torrents, {len(categories)} categories in qBittorrent.")

    # Category -> list of affected torrents (save_path doesn't match savePath)
    mismatches_by_cat = {}
    skipped_no_savepath = 0
    skipped_no_category = 0
    skipped_prefix = 0
    already_ok = 0

    for t in torrents:
        cat_name = t.get("category") or ""
        if not cat_name:
            skipped_no_category += 1
            continue

        if not category_allowed(cat_name):
            continue

        cat_info = categories.get(cat_name)
        if not cat_info:
            skipped_no_category += 1
            continue

        expected = norm(cat_info.get("savePath"))
        if not expected:
            skipped_no_savepath += 1
            continue

        actual = norm(t.get("save_path"))

        if actual == expected:
            already_ok += 1
            continue

        if skip_prefixes and matches_skip_prefix(actual, skip_prefixes):
            skipped_prefix += 1
            continue

        mismatches_by_cat.setdefault(cat_name, []).append(t)

    total_mismatched = sum(len(v) for v in mismatches_by_cat.values())

    logger.info("-" * 70)
    logger.info(f"Already correctly placed (per API): {already_ok}")
    logger.info(f"No category               : {skipped_no_category}")
    logger.info(f"Category without save path: {skipped_no_savepath}")
    logger.info(f"Ignored (skip prefix)     : {skipped_prefix}")
    logger.info(f"Misplaced (per API save_path): {total_mismatched}")
    logger.info("-" * 70)

    # --------------------------------------------------------------------
    # Additionally: physically scan the filesystem. qBittorrent doesn't
    # always update save_path reliably if an internal move fails or is
    # aborted - the torrent then looks "correctly" placed per the API,
    # even though the files are actually still sitting in the wrong
    # category folder. This scan catches that as well.
    # --------------------------------------------------------------------
    physical_extra_count = 0
    if not args.no_scan_filesystem or extra_scan_dirs:
        category_local_dirs = build_category_local_dirs(categories, mappings)
        content_index = build_content_name_index(torrents)
        physical_mismatches = scan_physical_mismatches(
            category_local_dirs, extra_scan_dirs, content_index, skip_prefixes, logger,
            scan_category_dirs=not args.no_scan_filesystem
        )
        for cat_name, extra_list in physical_mismatches.items():
            if not category_allowed(cat_name):
                continue
            existing_hashes = {t["hash"] for t in mismatches_by_cat.get(cat_name, [])}
            for t in extra_list:
                if t["hash"] in existing_hashes:
                    continue
                mismatches_by_cat.setdefault(cat_name, []).append(t)
                existing_hashes.add(t["hash"])
                physical_extra_count += 1

        logger.info(f"Additionally found via filesystem scan (physically wrong, "
                    f"API save_path falsely suggested 'correct'): {physical_extra_count}")
        logger.info("-" * 70)

    total_mismatched = sum(len(v) for v in mismatches_by_cat.values())

    if not args.no_scan_filesystem or extra_scan_dirs:
        logger.info(f"Total misplaced (API + filesystem scan): {total_mismatched}")
        logger.info("-" * 70)

    if not mismatches_by_cat:
        logger.info("Nothing to do. Done.")
        return

    # Overview: which "from -> to" moves occur how often
    move_pairs = {}
    for cat_name, cat_torrents in mismatches_by_cat.items():
        for t in cat_torrents:
            current_cat = guess_current_category(t.get("save_path"), reverse_lookup) or "(root/unknown)"
            key = (current_cat, cat_name)
            move_pairs[key] = move_pairs.get(key, 0) + 1

    logger.info("Overview of required moves (from -> to: count):")
    for (src, dst), count in sorted(move_pairs.items(), key=lambda x: -x[1]):
        logger.info(f"    {src}  ->  {dst}: {count}")
    logger.info("-" * 70)

    should_pause_all = (not args.dry_run) and (not args.no_pause_all_during_run)
    if should_pause_all:
        logger.info("Pausing ALL torrents in qBittorrent before moving files ...")
        client.pause_all()

    try:
        for cat_name, cat_torrents in sorted(mismatches_by_cat.items()):
            expected = norm(categories[cat_name].get("savePath"))
            logger.info("")
            logger.info(f"### Category '{cat_name}' -> target: {expected}  ({len(cat_torrents)} torrent(s) affected)")
            for t in cat_torrents:
                if "_physical_src" in t:
                    where = (f"PHYSICALLY found in category folder '{t['_physical_found_in_category']}' "
                             f"({t['_physical_src']}) - API reports save_path={t.get('save_path')} "
                             f"(may falsely look correct per the API)")
                else:
                    current_cat = guess_current_category(t.get("save_path"), reverse_lookup)
                    if current_cat and current_cat != cat_name:
                        where = f"currently in category '{current_cat}' ({t.get('save_path')})"
                    elif current_cat is None:
                        where = f"currently in no known category folder ({t.get('save_path')})"
                    else:
                        where = f"current: {t.get('save_path')}"
                logger.info(f"    - {t['name']}  [{where}] -> should go to '{cat_name}'")

            if args.dry_run:
                # In dry-run, still show the planned file merge including
                # conflict detection (read-only, nothing gets changed).
                for t in cat_torrents:
                    content_path = t.get("content_path") or t.get("save_path")
                    if "_physical_src" in t:
                        # Found via filesystem scan: use the actually
                        # verified physical path, NOT the (possibly
                        # misleading) API content_path.
                        local_src = t["_physical_src"]
                    else:
                        local_src = translate_path(content_path, mappings)
                    local_dst = translate_path(
                        os.path.join(expected, os.path.basename(content_path)),
                        mappings
                    )
                    logger.debug(f"    [dry-run] Planned merge: {local_src} -> {local_dst}")
                    merge_move(local_src, local_dst, logger, dry_run=True)
                continue

            for batch in chunked(cat_torrents, args.batch_size):
                hashes = [t["hash"] for t in batch]

                # 0) Move/merge files locally into the destination folder
                #    already, BEFORE qBittorrent gets involved at all.
                #    Conflicts (same filename at destination) are resolved
                #    by size comparison: the larger file survives.
                merge_failed = set()
                for t in batch:
                    content_path = t.get("content_path") or t.get("save_path")
                    if "_physical_src" in t:
                        local_src = t["_physical_src"]
                    else:
                        local_src = translate_path(content_path, mappings)
                    local_dst = translate_path(
                        os.path.join(expected, os.path.basename(content_path)),
                        mappings
                    )
                    logger.info(f"    Merge: {local_src} -> {local_dst}")
                    ok, moved = merge_move(local_src, local_dst, logger, dry_run=False)
                    if not ok:
                        merge_failed.add(t["hash"])
                        logger.warning(f"    Merge for '{t['name']}' incomplete/failed - "
                                        f"will NOT be rechecked/re-enabled, please check manually.")

                hashes = [h for h in hashes if h not in merge_failed]
                if not hashes:
                    continue

                # 1) Disable AutoTMM to avoid the recalculation race
                logger.debug(f"    Disabling AutoTMM for {len(hashes)} torrent(s)")
                client.set_auto_management(hashes, enable=False)
                time.sleep(1)

                # 2) Tell qBittorrent about the new (now already physically
                #    correct) location
                logger.info(f"    setLocation -> {expected} for {len(hashes)} torrent(s)")
                client.set_location(hashes, expected)

                # 3) Wait until the move has completed
                deadline = time.time() + args.poll_timeout
                pending = set(hashes)
                moved_ok = []
                while pending and time.time() < deadline:
                    time.sleep(args.poll_interval)
                    current = {t["hash"]: t for t in client.get_torrents_by_hashes(list(pending))}
                    for h in list(pending):
                        info = current.get(h)
                        if info is None:
                            # Torrent no longer found (removed?) - drop it from the queue
                            pending.discard(h)
                            continue
                        state = info.get("state", "")
                        if norm(info.get("save_path")) == expected and "moving" not in state:
                            moved_ok.append(h)
                            pending.discard(h)

                if pending:
                    logger.warning(f"    Timeout waiting for move of {len(pending)} torrent(s): "
                                    f"{sorted(pending)}. Will NOT be rechecked/re-enabled - please check manually.")

                if moved_ok:
                    logger.info(f"    {len(moved_ok)} torrent(s) moved successfully.")

                    if not args.no_recheck:
                        logger.info(f"    Starting recheck for {len(moved_ok)} torrent(s)")
                        client.recheck(moved_ok)

                    if not args.no_reenable_autotmm:
                        logger.debug(f"    Re-enabling AutoTMM for {len(moved_ok)} torrent(s)")
                        time.sleep(1)
                        client.set_auto_management(moved_ok, enable=True)
    finally:
        if should_pause_all:
            logger.info("")
            logger.info("Resuming ALL torrents in qBittorrent (moving/recheck complete) ...")
            try:
                client.resume_all()
            except Exception as e:
                logger.error(f"COULD NOT resume torrents ({e})! "
                              f"Please check qBittorrent manually and resume all torrents if needed.")
                raise

    logger.info("")
    logger.info("=" * 70)
    logger.info(f"Done. {'(DRY-RUN, nothing was changed)' if args.dry_run else ''}")
    logger.info("=" * 70)


if __name__ == "__main__":
    start = datetime.now()
    main()
    print(f"\nRuntime: {datetime.now() - start}")
