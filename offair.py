#!/usr/bin/python3
"""offair: overnight conversion of off-air TV recordings (MPEG-TS files such as the
.mpeg files fetchtv-cli saves from a Fetch TV box) to MP4 files a media server can play
directly, filed into TV and film library folders.

Usage:
  offair.py                  normal run (used by the LaunchAgent)
  offair.py --dry-run        list what would be converted and why, change nothing
  offair.py --force          ignore the run window (for testing)
  offair.py --limit N        convert at most N files this run
  offair.py check            test the settings and the setup, change nothing
  offair.py install          install (or update) the nightly job and the Quick Action
  offair.py uninstall        remove them again

The "Convert now" Quick Action drops a request file in REQUESTS_DIR and kickstarts the
LaunchAgent. Requested files go first, and outside the run window they are the only ones
converted (on battery too); each gets a notification when it's filed or has failed.

Written for /usr/bin/python3 (3.9) so the interpreter path never changes.
"""

import argparse
import configparser
import datetime
import errno
import json
import logging
import logging.handlers
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata

# ---------------------------------------------------------------- settings --
# These are the defaults. Your own settings go in the config file (copy
# config.example.ini to CONFIG_FILE); anything set there replaces the value here.

HOME = os.path.expanduser("~")
CONFIG_FILE = os.environ.get("OFFAIR_CONFIG") or os.path.join(HOME, ".config/offair/config.ini")

# Where the recordings are, and where the MP4s go
SOURCE_DIR = ""                    # required
TV_DIR = ""                        # required while FILE_INTO_LIBRARY is on
MOVIES_DIR = ""                    # required while FILE_INTO_LIBRARY is on
SOURCE_EXTENSIONS = (".mpeg",)

# Network share holding those folders. Leave all three empty if they are on local disks.
MOUNT_POINT = ""                   # e.g. /Volumes/Media
SHARE_URL = ""                     # e.g. smb://user@nas._smb._tcp.local/Media, as saved in the Keychain
NAS_HOST = ""                      # e.g. nas.local
NAS_PORT = 445

# Local working files, state and log
WORK_DIR = os.path.join(HOME, "Library/Caches/offair")
STATE_FILE = os.path.join(HOME, "Library/Application Support/offair/state.json")
REQUESTS_DIR = os.path.join(HOME, "Library/Application Support/offair/requests")
AD_REQUESTS_DIR = os.path.join(HOME, "Library/Application Support/offair/ad-requests")
REDO_REQUESTS_DIR = os.path.join(HOME, "Library/Application Support/offair/redo-requests")
# In the recordings folder, for the download script (see extras/fetch-sync.sh): the
# recordings to download again, and those it couldn't find on the box.
REFETCH_LIST = "refetch-requests.txt"
REFETCH_FAILED = "refetch-failed.txt"
LOG_FILE = os.path.join(HOME, "Library/Logs/offair.log")
LOG_MAX_BYTES = 1_000_000          # rotate the log at about 1 MB
LOG_BACKUPS = 3                    # keep offair.log.1 .. .3

# Run window (24-hour clock). A file that has started is always allowed to finish.
RUN_START = "04:00"
RUN_END = "07:30"
LAUNCH_AT = "04:30"                # when the LaunchAgent starts the nightly run

# Conversion (the tested baseline: yadif, libx264 CRF 20 preset fast, AAC 192k)
DEINTERLACE = "yadif"              # ffmpeg filter, e.g. "yadif" or "bwdif"; "" = none
CRF = 20
PRESET = "fast"
AUDIO_BITRATE = "192k"
PREFERRED_AUDIO = ("ac3", "eac3")  # use these if present, else the first audio stream

# Optional faster mode: Apple hardware encoder. UNTESTED - leave off until compared.
USE_HARDWARE_ENCODER = False
HW_VIDEO_BITRATE = "8M"

# Ad breaks: Comskip finds them and each is marked as an "Ads" chapter in the MP4.
# Nothing is cut out. If Comskip is missing, fails or finds nothing, the MP4 simply
# has no chapters.
DETECT_ADS = False
COMSKIP = os.path.join(HOME, ".local/bin/comskip")
COMSKIP_INI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extras", "comskip.ini")
COMSKIP_TIMEOUT_FACTOR = 1         # give up on Comskip after 1 x the running time

# Originals are kept until you turn this off, after a week or so of good runs.
KEEP_ORIGINALS = True

# Filing: checked MP4s move from SOURCE_DIR into the libraries. Episodes (names like
# S16_E10_...) go to TV_DIR/<Show>/Season N/, everything else to MOVIES_DIR/<Title>/.
FILE_INTO_LIBRARY = True

# Which files qualify
MIN_SIZE_MB = 50
MIN_AGE_MINUTES = 20               # not modified for this long
STABLE_CHECK_SECONDS = 30          # and size unchanged over this long

# Checks and limits
DURATION_TOLERANCE = 0.02          # output duration within 2% of the source
MAX_FAILURES = 3                   # stop trying a file after this many failures
FFMPEG_TIMEOUT_FACTOR = 4          # kill ffmpeg after 4 x the recording's running time
SHARE_DOWN_ALERT_NIGHTS = 3        # notify after this many nights in a row without the share
COPY_STALL_MINUTES = 10            # stop the run if a copy makes no progress for this long

# Status page (offair_status.py), on this Mac only: http://localhost:<port>
STATUS_PORT = 8765
STATUS_ON_NETWORK = False          # let other devices on the local network open the status page
STATUS_PASSWORD = ""               # which they must give; required while that is on

# Plex's own ad detection, which gives a Skip Commercials button (see README).
PLEX_ADS = False
PLEX_URL = ""                      # e.g. http://nas.local:32400
PLEX_TOKEN_FILE = os.path.join(HOME, ".config/offair/plex-token")
PLEX_FIND_MINUTES = 10             # wait this long for Plex to notice newly filed MP4s
PLEX_SCAN_FACTOR = 2.5             # wait up to 2.5 x the running time for each scan
PLEX_POLL_SECONDS = 60

NOTIFY_TITLE = "Offair"

# Config file layout: [section] key -> the setting above, and how to read its value.
CONFIG_KEYS = {
    "folders": {"recordings": ("SOURCE_DIR", "path"), "tv": ("TV_DIR", "path"),
                "movies": ("MOVIES_DIR", "path"), "extensions": ("SOURCE_EXTENSIONS", "list")},
    "share": {"mount_point": ("MOUNT_POINT", "path"), "url": ("SHARE_URL", "text"),
              "host": ("NAS_HOST", "text"), "port": ("NAS_PORT", "int")},
    "schedule": {"window_start": ("RUN_START", "time"), "window_end": ("RUN_END", "time"),
                 "launch_at": ("LAUNCH_AT", "time")},
    "encode": {"deinterlace": ("DEINTERLACE", "text"), "crf": ("CRF", "int"),
               "preset": ("PRESET", "text"), "audio_bitrate": ("AUDIO_BITRATE", "text"),
               "preferred_audio": ("PREFERRED_AUDIO", "list"),
               "hardware_encoder": ("USE_HARDWARE_ENCODER", "bool"),
               "hardware_bitrate": ("HW_VIDEO_BITRATE", "text")},
    "ads": {"detect": ("DETECT_ADS", "bool"), "comskip": ("COMSKIP", "path"),
            "comskip_ini": ("COMSKIP_INI", "path")},
    "files": {"keep_originals": ("KEEP_ORIGINALS", "bool"),
              "file_into_library": ("FILE_INTO_LIBRARY", "bool"),
              "min_size_mb": ("MIN_SIZE_MB", "int"), "min_age_minutes": ("MIN_AGE_MINUTES", "int"),
              "max_failures": ("MAX_FAILURES", "int")},
    "notifications": {"title": ("NOTIFY_TITLE", "text"),
                      "share_down_nights": ("SHARE_DOWN_ALERT_NIGHTS", "int")},
    "status": {"port": ("STATUS_PORT", "int"), "on_network": ("STATUS_ON_NETWORK", "bool"),
               "password": ("STATUS_PASSWORD", "text")},
    "plex": {"detect_ads": ("PLEX_ADS", "bool"), "url": ("PLEX_URL", "text"),
             "token_file": ("PLEX_TOKEN_FILE", "path")},
}

# ------------------------------------------------------------------------------

LABEL = "offair.convert"
STATUS_LABEL = "offair.status"
QUICK_ACTION = "Convert now.workflow"
PYTHON = "/usr/bin/python3"        # Apple's, so its path and privacy permission never change
# The same PATH for launchd and Terminal runs: Homebrew (Apple silicon, then Intel), then macOS.
TOOL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
os.environ["PATH"] = TOOL_PATH

LOCK_DIR = os.path.join(WORK_DIR, "run.lock")
FFMPEG_LOG = os.path.join(WORK_DIR, "ffmpeg-last.log")

log = logging.getLogger("offair")


class Stop(Exception):
    """Conditions changed (power, share, window, disk): stop the run, not the file's fault."""


class FileFailed(Exception):
    """This file could not be converted; counts towards MAX_FAILURES."""


def load_config(path=None):
    """Apply the config file to the settings above. Returns a list of problems, which
    is empty if the settings are usable."""
    path = path or CONFIG_FILE
    problems = []
    cp = configparser.ConfigParser(interpolation=None)
    try:
        with open(path) as f:
            cp.read_file(f)
    except FileNotFoundError:
        return ["no config file at %s (copy config.example.ini there and edit it)" % path]
    except (OSError, configparser.Error) as e:
        return ["cannot read %s: %s" % (path, str(e).replace("\n", " "))]
    for section in cp.sections():
        for key, raw in cp.items(section):
            where = "[%s] %s" % (section, key)
            if key not in CONFIG_KEYS.get(section, {}):
                problems.append("%s is not a setting" % where)
                continue
            name, kind = CONFIG_KEYS[section][key]
            raw = raw.strip()
            if kind == "path":
                value = os.path.expanduser(raw).rstrip("/") if raw else ""
            elif kind == "list":
                value = tuple(x.strip() for x in raw.split(",") if x.strip())
            elif kind == "int":
                if not raw.isdigit():
                    problems.append("%s must be a whole number, not %r" % (where, raw))
                    continue
                value = int(raw)
            elif kind == "bool":
                if raw.lower() not in ("yes", "no", "true", "false", "on", "off"):
                    problems.append("%s must be yes or no, not %r" % (where, raw))
                    continue
                value = raw.lower() in ("yes", "true", "on")
            elif kind == "time":
                if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", raw):
                    problems.append("%s must be a 24-hour time like 04:30, not %r" % (where, raw))
                    continue
                value = raw
            else:
                value = raw
            globals()[name] = value
    if not SOURCE_DIR:
        problems.append("[folders] recordings is not set")
    if FILE_INTO_LIBRARY and not (TV_DIR and MOVIES_DIR):
        problems.append("[folders] tv and movies must be set (or set [files] file_into_library = no)")
    if not SOURCE_EXTENSIONS or not all(e.startswith(".") for e in SOURCE_EXTENSIONS):
        problems.append("[folders] extensions must be a list like .mpeg, .ts")
    if STATUS_ON_NETWORK and len(STATUS_PASSWORD) < 6:
        problems.append("[status] password must be set (6 characters or more) while on_network = yes")
    if PLEX_ADS and not PLEX_URL.startswith(("http://", "https://")):
        problems.append("[plex] url must be set, like http://nas.local:32400, while detect_ads = yes")
    if PLEX_ADS and not FILE_INTO_LIBRARY:
        problems.append("[plex] detect_ads needs [files] file_into_library = yes")
    return problems


# ---------------------------------------------------------------- helpers --

def setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES,
                                              backupCount=LOG_BACKUPS)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)


def notify(message):
    log.info("Notification: %s", message)
    subprocess.run(["osascript", "-e", "on run argv",
                    "-e", 'display notification (item 1 of argv) with title "%s"' % NOTIFY_TITLE,
                    "-e", "end run", message],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    except ValueError:
        bad = STATE_FILE + ".bad"
        os.replace(STATE_FILE, bad)
        log.warning("State file was unreadable; moved it to %s and started afresh", bad)
        state = {}
    state.setdefault("files", {})
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def gb(n):
    return "%.2f GB" % (n / 1e9)


# ------------------------------------------------------------------ gates --

def acquire_lock():
    """mkdir-based lock. Returns False if another live run holds it."""
    os.makedirs(WORK_DIR, exist_ok=True)
    pid_file = os.path.join(LOCK_DIR, "pid")
    for _ in range(2):
        try:
            os.mkdir(LOCK_DIR)
            with open(pid_file, "w") as f:
                f.write(str(os.getpid()))
            return True
        except FileExistsError:
            try:
                with open(pid_file) as f:
                    pid = int(f.read().strip())
            except (OSError, ValueError):
                # Lock just being created by another run, or left half-made by a crash.
                if time.time() - os.path.getmtime(LOCK_DIR) < 60:
                    return False
                pid = None
            if pid is not None:
                try:
                    os.kill(pid, 0)
                    return False            # that run is still alive
                except ProcessLookupError:
                    pass
                except PermissionError:
                    return False
            log.info("Removing stale lock left by process %s", pid)
            shutil.rmtree(LOCK_DIR, ignore_errors=True)
    return False


def release_lock():
    shutil.rmtree(LOCK_DIR, ignore_errors=True)


def in_window():
    now = datetime.datetime.now().strftime("%H:%M")
    if RUN_START <= RUN_END:
        return RUN_START <= now < RUN_END
    return now >= RUN_START or now < RUN_END     # window crosses midnight


def on_mains_power():
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
    return "'AC Power'" in out


def nas_reachable():
    # nc rather than a Python socket: under launchd, macOS Local Network privacy
    # blocks python3's own connections ("No route to host") but not Apple's nc.
    if not NAS_HOST:
        return True
    r = subprocess.run(["nc", "-z", "-G", "3", NAS_HOST, str(NAS_PORT)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def share_mounted():
    """True only if the share is mounted at exactly MOUNT_POINT (or no share is set)."""
    if not MOUNT_POINT:
        return True
    if not os.path.ismount(MOUNT_POINT):
        return False
    if not SHARE_URL:
        return True
    out = subprocess.run(["mount"], capture_output=True, text=True).stdout
    share = SHARE_URL.rstrip("/").rsplit("/", 1)[-1]
    for line in out.splitlines():
        if " on %s (" % MOUNT_POINT in line:
            return line.split(" on ")[0].rstrip("/").endswith("/" + share)
    return False


def mount_share():
    if not SHARE_URL:
        return False
    log.info("Share not connected; trying to mount %s", SHARE_URL)
    try:
        subprocess.run(["osascript", "-e", 'mount volume "%s"' % SHARE_URL],
                       capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        log.info("Mount attempt timed out")
    if share_mounted():
        return True
    others = [v for v in os.listdir("/Volumes") if v.startswith(os.path.basename(MOUNT_POINT))]
    log.info("Share still not at %s (volumes seen: %s)", MOUNT_POINT, ", ".join(others) or "none")
    return False


def share_ok():
    return share_mounted() and os.path.isdir(SOURCE_DIR)


def record_share_down(state, nightly):
    """Count consecutive nights without the NAS; notify once at the threshold."""
    if not nightly:
        return
    today = datetime.date.today()
    down = state.get("share_down", {})
    last = down.get("last_night")
    if last == today.isoformat():
        return
    if last == (today - datetime.timedelta(days=1)).isoformat():
        down["nights"] = down.get("nights", 0) + 1
    else:
        down = {"nights": 1, "notified": False}
    down["last_night"] = today.isoformat()
    state["share_down"] = down
    if down["nights"] >= SHARE_DOWN_ALERT_NIGHTS and not down.get("notified"):
        notify("The recordings share has been unreachable for %d nights running; nothing is "
               "being converted." % down["nights"])
        down["notified"] = True
    save_state(state)


# ----------------------------------------------------------------- choose --

def case_clashes(names):
    """Names that differ from a sibling only in case. The NAS keeps them apart but
    macOS's SMB client can resolve one to the other mid-run, so leave them alone."""
    seen = {}
    for n in names:
        seen.setdefault(n.lower(), []).append(n)
    return {n for group in seen.values() if len(group) > 1 for n in group}


def find_sources():
    """Return (sources, clashes): clashes are paths skipped because of case_clashes."""
    found, clashes = [], []
    for root, dirs, files in os.walk(SOURCE_DIR):
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "@")))
        bad = case_clashes(dirs + files)
        clashes += [os.path.join(root, n) for n in sorted(bad)]
        dirs[:] = [d for d in dirs if d not in bad]
        for name in files:
            if name in bad:
                continue
            if not name.startswith(".") and name.lower().endswith(SOURCE_EXTENSIONS):
                found.append(os.path.join(root, name))
    return found, clashes


def mp4_path(src):
    return os.path.splitext(src)[0] + ".mp4"


def rel(path):
    return os.path.relpath(path, SOURCE_DIR)


def request_key(path):
    # Finder and os.walk can spell accented names differently; compare in NFC.
    return unicodedata.normalize("NFC", rel(path))


def read_requests():
    """Convert-now requests from the Quick Action: {request_key: [request files]}."""
    found = {}
    try:
        names = sorted(n for n in os.listdir(REQUESTS_DIR) if not n.startswith("."))
    except FileNotFoundError:
        return found
    for n in names:
        req = os.path.join(REQUESTS_DIR, n)
        try:
            with open(req) as f:
                path = f.read().strip()
        except OSError:
            continue
        if path:
            found.setdefault(request_key(path), []).append(req)
    return found


def finish_request(requests, key, message):
    """Drop the request files for key (including repeat requests made since the run
    started) and send a notification saying how it went."""
    requests.pop(key, None)
    for req in read_requests().get(key, []):
        try:
            os.remove(req)
        except OSError:
            pass
    notify(message)


def choose(state, requests=(), only_requests=False):
    """Return (candidates, skipped, clashes); skipped is a list of (path, reason).
    Requested files skip the age and failure-count limits and go first."""
    now = time.time()
    candidates, skipped = [], []
    sources, clashes = find_sources()
    for path in clashes:
        skipped.append((path, "name differs from another only in case; macOS can't tell "
                              "them apart over SMB, so merge or rename on the NAS"))
    for src in sources:
        asked = request_key(src) in requests
        if only_requests and not asked:
            continue
        entry = state["files"].get(rel(src), {})
        again = bool(entry.get("redo"))            # asked for on the status page
        try:
            st = os.stat(src)
        except OSError as e:
            skipped.append((src, "cannot read: %s" % e))
            continue
        if os.path.exists(mp4_path(src)) and not again:
            reason = "already converted (.mp4 exists)"
        elif entry.get("status") == "done" and not again:
            reason = "already converted" + (" and filed as %s" % entry["filed"]
                                            if "filed" in entry else "")
        elif entry.get("failures", 0) >= MAX_FAILURES and not (asked or again):
            reason = "gave up after %d failures: %s" % (entry["failures"], entry.get("last_error", ""))
        elif st.st_size < MIN_SIZE_MB * 1e6:
            reason = "too small (%d MB)" % (st.st_size / 1e6)
        elif now - st.st_mtime < MIN_AGE_MINUTES * 60 and not asked:
            reason = "modified in the last %d minutes (still downloading?)" % MIN_AGE_MINUTES
        elif os.path.exists(src + ".lock"):
            reason = "fetchtv lock file present (still downloading?)"
        else:
            candidates.append((src, st.st_size, st.st_mtime))
            continue
        skipped.append((src, reason))

    if candidates:
        log.info("Checking %d candidate(s) are not growing (%d s)...", len(candidates),
                 STABLE_CHECK_SECONDS)
        time.sleep(STABLE_CHECK_SECONDS)
        stable = []
        for src, size, mtime in candidates:
            try:
                same = os.path.getsize(src) == size
            except OSError:
                same = False
            if same:
                stable.append((src, size, mtime))
            else:
                skipped.append((src, "size changed during check (still downloading?)"))
        candidates = stable

    # Requested files first, then oldest recording first
    candidates.sort(key=lambda c: (request_key(c[0]) not in requests, c[2]))
    return [(src, size) for src, size, _ in candidates], skipped, clashes


# ---------------------------------------------------------------- convert --

def probe(path):
    """Return (duration_seconds, streams) or raise FileFailed."""
    r = subprocess.run(["ffprobe", "-v", "error", "-of", "json",
                        "-show_entries", "format=duration:stream=index,codec_type,codec_name:"
                                         "stream_disposition=attached_pic", path],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=300)
    if r.returncode != 0:
        raise FileFailed("ffprobe could not read %s" % os.path.basename(path))
    info = json.loads(r.stdout or "{}")
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, ValueError):
        raise FileFailed("ffprobe found no duration in %s" % os.path.basename(path))
    if duration <= 0:
        raise FileFailed("zero duration in %s" % os.path.basename(path))
    return duration, info.get("streams", [])


def pick_streams(streams):
    video = [s for s in streams if s.get("codec_type") == "video"
             and not s.get("disposition", {}).get("attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        raise FileFailed("no video stream")
    if not audio:
        raise FileFailed("no audio stream")
    preferred = [s for s in audio if s.get("codec_name") in PREFERRED_AUDIO]
    return video[0], (preferred or audio)[0]


_copying = {"what": None, "at": 0.0}


def copy_file(src, dst):
    """Copy src to dst in chunks, noting progress as it goes, so that watch_copies can
    tell a slow copy from one that has hung (which a network share can do forever)."""
    _copying["at"], _copying["what"] = time.time(), src
    try:
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            while True:
                chunk = fin.read(8 << 20)
                if not chunk:
                    break
                fout.write(chunk)
                _copying["at"] = time.time()
    finally:
        _copying["what"] = None


def copy_stalled(what):
    """A hung copy can't be interrupted, so end the whole run. Nothing counts against the
    recording; the next run clears the lock and the leftovers and tries it again."""
    log.error("Copy of %s made no progress for %d minutes; ending this run", what, COPY_STALL_MINUTES)
    notify("Copying %s stalled, so tonight's run was stopped. It will be tried again next run."
           % os.path.basename(what))
    logging.shutdown()
    release_lock()
    os._exit(1)


def watch_copies(interval=30):
    def loop():
        while True:
            time.sleep(interval)
            what = _copying["what"]
            if what and time.time() - _copying["at"] > COPY_STALL_MINUTES * 60:
                copy_stalled(what)
    threading.Thread(target=loop, daemon=True).start()


def detect_ads(local_src, src_duration):
    """Ad breaks Comskip finds in local_src, as [(start, end)] in seconds. Any problem
    means no breaks: ad detection must never fail a conversion."""
    out_dir = os.path.join(WORK_DIR, "comskip")
    started = time.time()
    try:
        shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir)
        cmd = [COMSKIP, "--output=" + out_dir]
        if os.path.exists(COMSKIP_INI):
            cmd.append("--ini=" + COMSKIP_INI)
        # Comskip's exit status says whether it found ads, not whether it worked,
        # so the EDL file ("start end 0" per break, in seconds) is what counts.
        subprocess.run(cmd + [local_src], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=max(600, src_duration * COMSKIP_TIMEOUT_FACTOR))
        edl = os.path.join(out_dir, os.path.splitext(os.path.basename(local_src))[0] + ".edl")
        breaks = []
        with open(edl) as f:
            for line in f:
                if line.strip():
                    start, end = (float(x) for x in line.split()[:2])
                    # A break within 5 s of either end runs to that end.
                    start = 0.0 if start < 5 else start
                    end = src_duration if end > src_duration - 5 else end
                    if end > start:
                        breaks.append((start, end))
    except Exception as e:
        log.warning("Ad detection skipped: %s", e if isinstance(e, OSError) else repr(e))
        return []
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    log.info("Comskip took %d s and found %d ad break(s)%s", time.time() - started, len(breaks),
             ": " + ", ".join("%d:%02d-%d:%02d" % (s // 60, s % 60, e // 60, e % 60)
                              for s, e in breaks) if breaks else "")
    return sorted(breaks)


def write_chapters(breaks, duration, path):
    """Write an ffmpeg metadata file: "Ads" chapters for the breaks, "Part N" between."""
    chapters, pos, part = [], 0.0, 0
    for start, end in breaks + [(duration, duration)]:
        if start - pos >= 1:                    # programme between the breaks
            part += 1
            chapters.append((pos, start, "Part %d" % part))
        if end > start:
            chapters.append((start, end, "Ads"))
        pos = max(pos, end)
    with open(path, "w") as f:
        f.write(";FFMETADATA1\n")
        for start, end, title in chapters:
            f.write("[CHAPTER]\nTIMEBASE=1/1000\nSTART=%d\nEND=%d\ntitle=%s\n"
                    % (start * 1000, end * 1000, title))


def ffmpeg_command(src, out, v, a, chapters=None):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-fflags", "+genpts", "-i", src]
    if chapters:
        cmd += ["-i", chapters, "-map_chapters", "1"]
    cmd += ["-map", "0:%d" % v["index"], "-map", "0:%d" % a["index"]]
    if DEINTERLACE:
        cmd += ["-vf", DEINTERLACE]
    if USE_HARDWARE_ENCODER:
        cmd += ["-c:v", "h264_videotoolbox", "-b:v", HW_VIDEO_BITRATE]
    else:
        cmd += ["-c:v", "libx264", "-crf", str(CRF), "-preset", PRESET]
    # Fetch's 5.1 is "5.1(side)", which the AAC encoder can only store with a custom channel
    # map that many players play as silence; convert it to standard 5.1 first.
    cmd += ["-af", "aformat=channel_layouts=mono|stereo|5.1"]
    cmd += ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-movflags", "+faststart", out]
    return cmd


def tail(path, lines=15):
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-lines:]).strip()
    except OSError:
        return ""


def check_output(path, src_duration, chapters=False):
    duration, streams = probe(path)
    kinds = sorted(s.get("codec_type") for s in streams)
    # ffmpeg stores MP4 chapters in a data stream too, so allow one, but only then.
    if kinds != ["audio", "video"] and not (chapters and kinds == ["audio", "data", "video"]):
        raise FileFailed("output has streams %s, expected one video and one audio" % kinds)
    if abs(duration - src_duration) > src_duration * DURATION_TOLERANCE:
        raise FileFailed("output lasts %.0f s but source lasts %.0f s" % (duration, src_duration))
    return duration


def convert(src, size, state):
    name = os.path.basename(src)
    final = mp4_path(src)
    partial = final + ".partial"

    # Room for the local copy of the recording plus the MP4 (usually a little larger).
    free = shutil.disk_usage(WORK_DIR).free
    if free < 3 * size:
        raise Stop("only %s free on the Mac, need %s for %s" % (gb(free), gb(3 * size), name))

    # ffprobe and ffmpeg only ever see local files. Under launchd, macOS asks before
    # letting Homebrew's binaries read network volumes, and nobody answers at 4 am.
    local_src = os.path.join(WORK_DIR, "source" + os.path.splitext(name)[1])
    local = os.path.join(WORK_DIR, os.path.splitext(name)[0] + ".mp4")
    chapters = os.path.join(WORK_DIR, "chapters.txt")
    breaks = []
    try:
        copy_file(src, local_src)
        if os.path.getsize(local_src) != size:
            raise FileFailed("copy from NAS was incomplete")
        src_duration, streams = probe(local_src)
        v, a = pick_streams(streams)
        log.info("Converting %s (%s, %d min; video #%d %s, audio #%d %s)", rel(src), gb(size),
                 src_duration / 60, v["index"], v.get("codec_name"), a["index"], a.get("codec_name"))

        if DETECT_ADS:
            breaks = detect_ads(local_src, src_duration)
            if breaks:
                write_chapters(breaks, src_duration, chapters)

        started = time.time()
        with open(FFMPEG_LOG, "w") as errlog:
            r = subprocess.run(ffmpeg_command(local_src, local, v, a, chapters if breaks else None),
                               stdout=subprocess.DEVNULL, stderr=errlog,
                               timeout=max(3600, src_duration * FFMPEG_TIMEOUT_FACTOR))
        if r.returncode != 0:
            raise FileFailed("ffmpeg exited with %d: %s" % (r.returncode, tail(FFMPEG_LOG, 5)))
        elapsed = time.time() - started
        out_duration = check_output(local, src_duration, chapters=bool(breaks))
        out_size = os.path.getsize(local)
        log.info("Encoded in %d min (%.2fx real time), %s, duration %.0f s vs %.0f s",
                 elapsed / 60, src_duration / elapsed, gb(out_size), out_duration, src_duration)

        # Copy to the NAS under a .partial name, then rename into place.
        if not share_ok():
            raise Stop("share disconnected before copying %s" % name)
        if os.path.exists(final):
            raise FileFailed("%s appeared while converting; not overwriting" % os.path.basename(final))
        if os.path.exists(partial):
            os.remove(partial)
        try:
            copy_file(local, partial)
        except OSError as e:
            if e.errno == errno.ENOSPC:           # a full disk is not this file's fault
                raise Stop("no space left in %s for %s" % (os.path.dirname(final), name))
            raise
        if os.path.getsize(partial) != out_size:
            os.remove(partial)
            raise FileFailed("copy to NAS was incomplete")
        os.rename(partial, final)
        log.info("Saved %s", rel(final))
    except subprocess.TimeoutExpired as e:
        raise FileFailed("%s timed out after %d s" % (os.path.basename(e.cmd[0]), e.timeout))
    finally:
        for path in (local_src, local, chapters):
            if os.path.exists(path):
                os.remove(path)

    before = state["files"].get(rel(src), {})
    state["files"][rel(src)] = {
        "status": "done", "converted_at": now_iso(), "encode_minutes": round(elapsed / 60, 1),
        "source_bytes": size, "output_bytes": out_size,
        "encoder": "h264_videotoolbox" if USE_HARDWARE_ENCODER else "libx264",
    }
    if before.get("redo") and before.get("filed") and not before["filed"].startswith("no"):
        state["files"][rel(src)]["replace"] = before["filed"]      # converted again: swap it in
    if DETECT_ADS:
        state["files"][rel(src)]["ad_breaks"] = len(breaks)
    save_state(state)

    if not KEEP_ORIGINALS:
        os.remove(src)
        state["files"][rel(src)]["original_deleted"] = True
        save_state(state)
        log.info("Deleted original %s", rel(src))


def record_failure(src, error, state):
    entry = state["files"].setdefault(rel(src), {})
    entry["failures"] = entry.get("failures", 0) + 1
    entry["last_error"] = str(error)
    entry["last_failed_at"] = now_iso()
    entry["status"] = "failed"
    log.error("Failed (%d/%d) %s: %s", entry["failures"], MAX_FAILURES, rel(src), error)
    if entry["failures"] >= MAX_FAILURES:
        notify("Gave up converting %s after %d failures: %s"
               % (os.path.basename(src), entry["failures"], error))
    save_state(state)


# ----------------------------------------------------------------- filing --

EPISODE = re.compile(r"^S(\d+)_E(\d+)(?:_-_(.*))?$")
AIR_DATE = re.compile(r"_-_[A-Z][a-z]{2}_\d{2}_[A-Z][a-z]{2}$")     # fetchtv's "_-_Wed_07_Jan"


def tidy(name):
    return name.replace("_", " ").strip()


def media_rel(path):
    return os.path.relpath(path, os.path.dirname(SOURCE_DIR))


def existing_name(parent, name):
    """The entry in parent that matches name ignoring case, else name itself."""
    try:
        for entry in os.listdir(parent):
            if entry.lower() == name.lower():
                return entry
    except FileNotFoundError:
        pass
    return name


def library_target(src):
    """Return (target, already): where src's MP4 belongs, and any file or folder
    already in the library for the same episode or movie (None if there isn't one)."""
    stem = AIR_DATE.sub("", os.path.splitext(os.path.basename(src))[0])
    m = EPISODE.match(stem)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        show = existing_name(TV_DIR, tidy(rel(src).split(os.sep)[0]))
        show_dir = os.path.join(TV_DIR, show)
        tag = "S%02dE%02d" % (season, episode)
        title = tidy(m.group(3) or "")
        target = os.path.join(show_dir, "Season %d" % season,
                              "%s - %s%s.mp4" % (show, tag, " - " + title if title else ""))
        same = re.compile(r"(?i)S0*%dE0*%d(?!\d)" % (season, episode))
        for root, dirs, files in os.walk(show_dir):
            dirs[:] = [d for d in dirs if not d.startswith((".", "@"))]
            for f in files:
                if same.search(f) and not f.startswith("."):
                    return target, os.path.join(root, f)
        return target, None
    title = tidy(stem)
    movie_dir = os.path.join(MOVIES_DIR, existing_name(MOVIES_DIR, title))
    return os.path.join(movie_dir, title + ".mp4"), (movie_dir if os.path.exists(movie_dir) else None)


def file_finished(state, dry_run=False):
    """Move converted, checked MP4s from SOURCE_DIR into the libraries. Never overwrites,
    except to swap in a recording that was converted again from the status page."""
    if not FILE_INTO_LIBRARY:
        return
    for key, entry in sorted(state["files"].items()):
        if entry.get("status") != "done" or "filed" in entry:
            continue
        src = os.path.join(SOURCE_DIR, key)
        mp4 = mp4_path(src)
        if not os.path.exists(mp4):
            continue                                # moved by hand
        try:
            target, already = library_target(src)
            old = os.path.join(os.path.dirname(SOURCE_DIR), entry["replace"]) if entry.get("replace") else None
            if old and already in (old, os.path.dirname(old)):
                already = None                      # its own earlier copy, which this replaces
            else:
                old = None
            if already:
                log.info("Not filing %s: already in the library as %s", rel(mp4), media_rel(already))
                if not dry_run:
                    entry["filed"] = "no, already in library: %s" % media_rel(already)
                    save_state(state)
                continue
            if dry_run:
                log.info("FILE     %s -> %s", rel(mp4), media_rel(target))
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if old and os.path.exists(old):
                os.remove(old)                      # only now, with the checked MP4 ready to move in
            os.rename(mp4, target)
            entry["filed"] = media_rel(target)
            if PLEX_ADS:
                entry["ads"] = "pending"
                if old:
                    entry["ads_rescan"] = True      # Plex's markers are for the earlier copy
            entry.pop("replace", None)
            save_state(state)
            log.info("%s %s -> %s", "Replaced" if old else "Filed", rel(mp4), media_rel(target))
        except OSError as e:
            log.warning("Could not file %s (%s); will try again next run", rel(mp4), e)
            if not share_ok():
                return


# --------------------------------------------------------- convert again --
# From the status page. If the original is still in the recordings folder it is simply
# converted again. If it was deleted, its name goes on a list in the recordings folder
# that the download script reads (see extras/fetch-sync.sh), and it is converted when
# the recording arrives. Either way the new MP4 replaces the one in the library.

def edit_refetch_list(key, add):
    path = os.path.join(SOURCE_DIR, REFETCH_LIST)
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    lines = [l for l in lines if l != key] + ([key] if add else [])
    if lines:
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
    elif os.path.exists(path):
        os.remove(path)


def take_redo_requests(state):
    """Apply the status page's convert-again requests, and hear back from the download
    script about recordings that are no longer on the box."""
    requests = read_ad_requests(REDO_REQUESTS_DIR)
    try:
        for _, add, keys in requests:
            for key in keys:
                entry = state["files"].get(key, {})
                if add and entry.get("status") == "done" and not entry.get("redo"):
                    if not os.path.exists(os.path.join(SOURCE_DIR, key)):
                        edit_refetch_list(key, True)
                        log.info("Convert again: asked the download script for %s", key)
                    else:
                        log.info("Convert again: %s", key)
                    entry["redo"] = now_iso()
                elif not add and entry.get("redo"):
                    edit_refetch_list(key, False)
                    del entry["redo"]
                    log.info("Convert again cancelled: %s", key)
        failed = os.path.join(SOURCE_DIR, REFETCH_FAILED)
        if os.path.exists(failed):
            with open(failed) as f:
                gone = [k for k in f.read().splitlines() if state["files"].get(k, {}).pop("redo", None)]
            os.remove(failed)
            if gone:
                notify("Can't convert again, no longer on the Fetch box: %s"
                       % ", ".join(os.path.basename(k) for k in gone))
    except OSError as e:
        log.warning("Could not apply convert-again requests (%s); will try again next run", e)
        return
    save_state(state)
    for req, _, _ in requests:
        try:
            os.remove(req)
        except OSError:
            pass


# --------------------------------------------------------------- Plex ads --
# Plex only scans a library for ads when its "Ad detection" setting is "For all items",
# and then it scans every item in it during maintenance: days of work for a small NAS.
# So the setting is switched on for the moment it takes to ask for one recording and
# then put back. Plex drops requests that queue behind a running scan once the setting
# is back, so recordings are done one at a time.

def plex(path, method="GET", **params):
    """Call the Plex server and return the MediaContainer of its answer. Uses Apple's
    curl, because macOS blocks Python's own sockets on the local network under launchd.
    The token goes to curl on stdin so it never shows in the process list."""
    with open(PLEX_TOKEN_FILE) as f:
        token = f.read().strip()
    cmd = ["/usr/bin/curl", "-sS", "-f", "-m", "30", "-K", "-", "-X", method, "-G",
           PLEX_URL.rstrip("/") + path]
    for k, v in params.items():
        cmd += ["--data-urlencode", "%s=%s" % (k, v)]
    r = subprocess.run(cmd, input='header = "X-Plex-Token: %s"\nheader = "Accept: application/json"\n'
                       % token, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise OSError("Plex %s %s: %s" % (method, path, r.stderr.strip() or r.returncode))
    try:
        return json.loads(r.stdout).get("MediaContainer", {})
    except ValueError:
        return {}


def plex_setting(path, name):
    for setting in plex(path).get("Setting", []):
        if setting.get("id") == name:
            return str(setting.get("value"))
    raise OSError("Plex has no %s setting at %s" % (name, path))


def plex_find(filed):
    """Plex's id for a filed MP4, or None if Plex hasn't added it yet."""
    full = os.path.join(os.path.dirname(SOURCE_DIR), filed)
    is_tv = full.startswith(TV_DIR + os.sep)
    tail = os.path.relpath(full, TV_DIR if is_tv else MOVIES_DIR)
    for item in plex("/library/all", type=4 if is_tv else 1, file=tail).get("Metadata", []):
        for media in item.get("Media", []):
            for part in media.get("Part", []):
                if part.get("file", "").replace("\\", "/").endswith("/" + tail):
                    return item["ratingKey"]
    return None


def plex_item(item_id):
    """(the item's details, the ad breaks Plex has marked in it as (start, end) pairs)"""
    item = plex("/library/metadata/%s" % item_id, includeMarkers=1)["Metadata"][0]
    return item, [(m.get("startTimeOffset"), m.get("endTimeOffset"))
                  for m in item.get("Marker", []) if m.get("type") == "commercial"]


def plex_start_scan(item):
    """Ask Plex to scan one item, with its library's setting on only for the request."""
    prefs = "/library/sections/%s/prefs" % item["librarySectionID"]
    before = plex_setting(prefs, "enableAdMarkerGeneration")
    if before != "2":
        plex(prefs, "PUT", enableAdMarkerGeneration=2)
    try:
        plex("/library/metadata/%s/addetect" % item["ratingKey"], "PUT")
    finally:
        if before != "2":
            plex(prefs, "PUT", enableAdMarkerGeneration=before)


def read_ad_requests(folder=None):
    """The status page's requests, oldest first: [(request file, add or remove, keys)]."""
    folder = folder or AD_REQUESTS_DIR
    try:
        names = sorted(n for n in os.listdir(folder) if not n.startswith("."))
    except FileNotFoundError:
        return []
    found = []
    for n in names:
        try:
            with open(os.path.join(folder, n)) as f:
                found.append((os.path.join(folder, n), not n.endswith(".remove"),
                              f.read().splitlines()))
        except OSError:
            continue
    return found


def apply_ad_requests(files, requests):
    """Queue recordings for Plex's ad detection, or take queued ones off again, in
    files (the state's entries). A scan that has started can't be taken back. Returns
    how many entries changed."""
    changed = 0
    for _, add, keys in requests:
        for key in keys:
            entry = files.get(key, {})
            if not entry.get("filed") or entry["filed"].startswith("no"):
                continue
            if add and entry.get("ads") not in ("pending", "scanning"):
                if "ads" in entry:
                    entry["ads_was"] = entry["ads"]          # put back if it's taken off again
                entry["ads"] = "pending"
                changed += 1
            elif not add and entry.get("ads") == "pending":
                if "ads_was" in entry:
                    entry["ads"] = entry.pop("ads_was")
                else:
                    del entry["ads"]
                changed += 1
    return changed


def take_ad_requests(state):
    """Apply the status page's requests to the state. Returns how many entries changed."""
    requests = read_ad_requests()
    changed = apply_ad_requests(state["files"], requests)
    if changed:
        save_state(state)
        log.info("Ad detection queue changed for %d recording(s)", changed)
    for req, _, _ in requests:
        try:
            os.remove(req)
        except OSError:
            pass
    return changed


def plex_detect_ads(state, keep_going=lambda: True):
    """Have Plex find the ad breaks in newly filed recordings and any queued on the
    status page. Whatever goes wrong here is logged and tried again next run; it never
    affects a conversion."""
    left = set()                                 # tried in this run

    def queued():
        # "scanning" is left behind by a run that was stopped while it waited
        return [(k, e) for k, e in sorted(state["files"].items())
                if e.get("ads") in ("pending", "scanning") and k not in left]

    if not PLEX_ADS:
        return
    take_ad_requests(state)
    if not queued():
        return
    try:
        if plex_setting("/:/prefs", "GenerateAdMarkerBehavior") == "never":
            log.warning("Plex ad detection is off: set Settings > Library > Generate ad "
                        "video markers to 'as a scheduled task' on the Plex server")
            return
        give_up_finding = time.time() + PLEX_FIND_MINUTES * 60
        while True:
            take_ad_requests(state)              # the queue can change while a scan runs
            if not queued():
                return
            key, entry = queued()[0]
            left.add(key)
            item_id = plex_find(entry["filed"])
            while not item_id and time.time() < give_up_finding and keep_going():
                time.sleep(PLEX_POLL_SECONDS)
                item_id = plex_find(entry["filed"])
            if not item_id:
                log.info("Plex hasn't added %s yet; its ad breaks wait for the next run", entry["filed"])
                continue
            if not keep_going():
                log.info("Run window has opened or closed; ad detection carries over to the next run")
                return
            item, breaks = plex_item(item_id)
            stale = breaks if entry.get("ads_rescan") else []     # markers from a replaced copy
            if not breaks or stale:
                plex_start_scan(item)
                entry["ads"], entry["ads_since"] = "scanning", now_iso()
                save_state(state)
                log.info("Plex is scanning %s for ad breaks", entry["filed"])
                deadline = time.time() + max(300, item.get("duration", 0) / 1000 * PLEX_SCAN_FACTOR)
                while (not breaks or breaks == stale) and time.time() < deadline and keep_going():
                    time.sleep(PLEX_POLL_SECONDS)
                    breaks = plex_item(item_id)[1]
            if (not breaks or breaks == stale) and not keep_going():
                log.info("Run window has opened or closed; ad detection carries over to the next run")
                return
            entry["ads"] = "%d ad break(s)" % len(breaks) if breaks else "none found"
            for done_with in ("ads_since", "ads_was", "ads_rescan"):
                entry.pop(done_with, None)
            save_state(state)
            log.info("Plex ad breaks in %s: %s", entry["filed"], entry["ads"])
    except (OSError, LookupError, ValueError, subprocess.TimeoutExpired) as e:
        log.warning("Plex ad detection stopped (%s); will try again next run", e)


# ------------------------------------------------------------------- main --

def run(args, state):
    requests = {} if args.dry_run else read_requests()
    # A Quick Action request outside the window converts only the requested files.
    only_requests = bool(requests) and not args.force and not in_window()
    nightly = not (args.force or args.dry_run or only_requests)
    keep_going = lambda: args.force or only_requests or in_window()     # for the Plex step
    if requests:
        log.info("Convert-now request(s): %s", ", ".join(sorted(requests)))
    page_requests = not args.dry_run and bool(read_ad_requests() or read_ad_requests(REDO_REQUESTS_DIR))
    if page_requests and not (requests or args.force or in_window()):
        # The queue was changed on the status page. Plex does the work, so power and the
        # share don't matter; it stops when the window opens so that it never blocks the
        # nightly run.
        log.info("Outside run window %s-%s; only applying status page requests", RUN_START, RUN_END)
        if nas_reachable() and (share_mounted() or mount_share()) and os.path.isdir(SOURCE_DIR):
            take_redo_requests(state)
        plex_detect_ads(state, lambda: not in_window())
        return 0

    def cannot_start(reason):
        log.info("%s; exiting", reason)
        record_share_down(state, nightly)
        if requests:
            notify("Can't convert now: %s. Requested files will wait for the next run." % reason)
        return 0

    if not in_window():
        if args.dry_run:
            log.info("Outside run window %s-%s (ignored for dry run)", RUN_START, RUN_END)
        elif only_requests:
            log.info("Outside run window %s-%s; converting requested files only", RUN_START, RUN_END)
        elif not args.force:
            log.info("Outside run window %s-%s; exiting", RUN_START, RUN_END)
            return 0
    if not on_mains_power():
        if args.dry_run or only_requests:
            log.info("On battery power (ignored for %s)",
                     "dry run" if args.dry_run else "requested files")
        else:
            log.info("On battery power; exiting")
            return 0
    if not nas_reachable():
        return cannot_start("NAS %s:%d not reachable" % (NAS_HOST, NAS_PORT))
    if not share_mounted() and not mount_share():
        return cannot_start("Share not connected at %s" % MOUNT_POINT)
    if not os.path.isdir(SOURCE_DIR):
        return cannot_start("%s does not exist" % SOURCE_DIR)
    if state.pop("share_down", None):
        save_state(state)
    try:
        os.listdir(SOURCE_DIR)
    except PermissionError:
        log.error("macOS privacy settings block reading %s; allow /usr/bin/python3 under "
                  "Privacy & Security (see README)", SOURCE_DIR)
        notify("macOS is blocking access to the recordings. Allow /usr/bin/python3 under "
               "Privacy & Security (see the offair README).")
        return 0

    if not args.dry_run:
        take_redo_requests(state)
    candidates, skipped, clashes = choose(state, requests, only_requests)
    chosen = {request_key(src) for src, _ in candidates}
    reasons = {request_key(src): reason for src, reason in skipped}
    for key, entry in state["files"].items():      # converting again, original deleted
        if entry.get("redo") and not os.path.exists(os.path.join(SOURCE_DIR, key)):
            reasons[unicodedata.normalize("NFC", key)] = (
                "it will be downloaded from the Fetch box again first, then converted")
    for key in sorted(set(requests) - chosen):
        finish_request(requests, key, "Not converting %s%s: %s" % (
            os.path.basename(key), " yet" if "downloaded from the Fetch box" in reasons.get(key, "") else "",
            reasons.get(key, "it isn't a recording in the recordings folder")))
    for path in clashes:
        log.warning("Skipping %s: its name differs from another only in case", rel(path))
    if clashes and not args.dry_run:
        notify("Skipping %s: names that differ only in case confuse macOS. Merge or rename "
               "them on the NAS." % ", ".join(os.path.basename(p) for p in clashes))

    if args.dry_run:
        for src, reason in sorted(skipped):
            log.info("SKIP     %s: %s", rel(src), reason)
        for i, (src, size) in enumerate(candidates):
            over = args.limit is not None and i >= args.limit
            log.info("%s %s (%s)%s", "LATER   " if over else "CONVERT ", rel(src), gb(size),
                     ": over --limit" if over else "")
        file_finished(state, dry_run=True)
        log.info("Dry run: %d to convert, %d skipped. Keep originals: %s", len(candidates),
                 len(skipped), KEEP_ORIGINALS)
        return 0

    if not candidates:
        log.info("Nothing to convert (%d file(s) skipped)", len(skipped))
        file_finished(state)
        plex_detect_ads(state, keep_going)
        return 0
    log.info("%d file(s) to convert", len(candidates))
    for leftover in os.listdir(WORK_DIR):        # from a run that was killed mid-encode
        if leftover.endswith(".mp4") or leftover.startswith("source."):
            os.remove(os.path.join(WORK_DIR, leftover))

    # Keep the Mac awake for as long as this process lives.
    caffeinate = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
    done = 0
    try:
        for src, size in candidates:
            if args.limit is not None and done >= args.limit:
                log.info("Reached --limit %d", args.limit)
                break
            if not (args.force or only_requests) and not in_window():
                log.info("Run window has closed; the rest carry over to the next night")
                break
            if not only_requests and not on_mains_power():
                log.info("No longer on mains power; stopping")
                break
            if not share_ok():
                log.info("Share disconnected; stopping")
                break
            key = request_key(src)
            try:
                if os.path.getsize(src) != size:
                    log.info("Size of %s changed since the scan; leaving it for later", rel(src))
                    continue
                convert(src, size, state)
                done += 1
                if key in requests:
                    file_finished(state)
                    filed = state["files"][rel(src)].get("filed", "")
                    finish_request(requests, key, "%s is ready: %s" % (os.path.basename(src), filed)
                                   if filed and not filed.startswith("no") else
                                   "Converted %s (not filed)" % os.path.basename(src))
            except Stop as e:
                log.warning("Stopping: %s", e)
                if "free on the Mac" in str(e) or "no space left" in str(e):
                    notify("Not converting: %s" % e)
                break
            except Exception as e:
                # If the share or power went away, it's not the file's fault.
                if not share_ok():
                    log.warning("Share disconnected during %s (%s); stopping", rel(src), e)
                    break
                if not os.path.exists(src):
                    log.warning("%s disappeared (moved?); skipping it", rel(src))
                    if key in requests:
                        finish_request(requests, key, "Not converting %s: it was moved or deleted"
                                       % os.path.basename(src))
                    continue
                error = e if isinstance(e, FileFailed) else repr(e)
                record_failure(src, error, state)
                if key in requests:
                    finish_request(requests, key, "Couldn't convert %s: %s"
                                   % (os.path.basename(src), error))
        file_finished(state)
        if requests:
            notify("Didn't get to %s; the next run will do %s first." % (
                ", ".join(os.path.basename(k) for k in sorted(requests)),
                "it" if len(requests) == 1 else "them"))
    finally:
        caffeinate.terminate()
    log.info("Finished: %d converted", done)
    plex_detect_ads(state, keep_going)
    return 0


# ------------------------------------------------------------------ setup --

def agent_plist(label):
    return os.path.join(HOME, "Library/LaunchAgents", label + ".plist")


def agent_loaded(label):
    r = subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def load_agent(label, script, extra):
    """Write a LaunchAgent that runs script with PYTHON, and (re)load it. True if it loaded."""
    env = {"PATH": TOOL_PATH}
    if os.environ.get("OFFAIR_CONFIG"):
        env["OFFAIR_CONFIG"] = os.path.abspath(CONFIG_FILE)
    crash_log = os.path.join(os.path.dirname(LOG_FILE), label + ".launchd.log")
    agent = {"Label": label, "ProgramArguments": [PYTHON, script], "EnvironmentVariables": env,
             "StandardOutPath": crash_log, "StandardErrorPath": crash_log}
    agent.update(extra)
    path = agent_plist(label)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(agent, f)
    domain = "gui/%d" % os.getuid()
    subprocess.run(["launchctl", "bootout", "%s/%s" % (domain, label)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(10):                      # bootout takes a moment to finish
        r = subprocess.run(["launchctl", "bootstrap", domain, path], capture_output=True, text=True)
        if r.returncode == 0:
            return True
        time.sleep(0.5)
    print("Could not load %s: %s" % (label, r.stderr.strip() or r.returncode))
    return False


def check(problems):
    """Test the settings and the setup and print one line per test. Changes nothing.
    Returns 0 if a nightly run would work."""
    failed = []

    def test(ok, what, fix=""):
        print("%s  %s%s" % ("ok  " if ok else "FAIL", what, "" if ok or not fix else "\n      -> " + fix))
        if not ok:
            failed.append(what)
        return ok

    def note(what):
        print("      %s" % what)

    print("Settings: %s" % CONFIG_FILE)
    for p in problems:
        test(False, p)
    if problems:
        print("\nFix the settings first, then run check again.")
        return 1
    test(True, "settings file reads cleanly")

    print("\nTools")
    for tool in ("ffmpeg", "ffprobe"):
        test(shutil.which(tool), "%s found" % tool, "brew install ffmpeg")
    test(os.path.exists(PYTHON), "%s exists" % PYTHON, "xcode-select --install")
    if DETECT_ADS:
        test(os.access(COMSKIP, os.X_OK), "comskip at %s" % COMSKIP, "build it (see README), or set [ads] detect = no")
        test(os.path.exists(COMSKIP_INI), "comskip settings at %s" % COMSKIP_INI)

    print("\nFolders")
    if NAS_HOST:
        test(nas_reachable(), "%s answers on port %d" % (NAS_HOST, NAS_PORT), "is it switched on?")
    if MOUNT_POINT:
        test(share_mounted(), "share mounted at %s" % MOUNT_POINT,
             "connect to it in Finder once and save the password in the Keychain")
    if test(os.path.isdir(SOURCE_DIR), "recordings folder %s" % SOURCE_DIR):
        sources, clashes = find_sources()
        note("%d recording(s) with extension %s" % (len(sources), " or ".join(SOURCE_EXTENSIONS)))
        for c in clashes:
            test(False, "%s differs from a neighbour only in case" % rel(c), "merge or rename them")
        probe_file = os.path.join(SOURCE_DIR, ".offair-check")
        try:
            open(probe_file, "w").close()
            os.remove(probe_file)
            test(True, "can create and delete files there")
        except OSError as e:
            test(False, "can create and delete files there (%s)" % e.strerror)
    if FILE_INTO_LIBRARY:
        for label, d in (("TV", TV_DIR), ("films", MOVIES_DIR)):
            test(os.path.isdir(d) and os.access(d, os.W_OK), "%s library folder %s is writable" % (label, d))
    os.makedirs(WORK_DIR, exist_ok=True)
    free = shutil.disk_usage(WORK_DIR).free
    test(free > 20e9, "%s free on this Mac for working files" % gb(free), "a 2-hour HD film needs about 15 GB")

    if PLEX_ADS:
        print("\nPlex ad detection")
        try:
            behaviour = plex_setting("/:/prefs", "GenerateAdMarkerBehavior")
            test(True, "Plex answers at %s and accepts the token" % PLEX_URL)
            test(behaviour != "never", "Plex's 'Generate ad video markers' is on (%s)" % behaviour,
                 "Plex Settings > Library > Generate ad video markers > as a scheduled task")
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            test(False, "Plex answers at %s and accepts the token (%s)" % (PLEX_URL, e),
                 "check [plex] url, and that %s holds your Plex token" % PLEX_TOKEN_FILE)

    print("\nNightly job")
    test(os.path.exists(agent_plist(LABEL)) and agent_loaded(LABEL), "nightly job installed and loaded",
         "offair.py install")
    test(os.path.exists(agent_plist(STATUS_LABEL)) and agent_loaded(STATUS_LABEL),
         "status page installed and loaded (http://localhost:%d)" % STATUS_PORT, "offair.py install")
    test(os.path.isdir(os.path.join(HOME, "Library/Services", QUICK_ACTION)),
         "Convert now Quick Action installed", "offair.py install")
    note("window %s-%s, started at %s; originals are %s; ad-break chapters are %s"
         % (RUN_START, RUN_END, LAUNCH_AT, "kept" if KEEP_ORIGINALS else "deleted after conversion",
            "on" if DETECT_ADS else "off"))
    if MOUNT_POINT:
        note("launchd needs Full Disk Access for %s to read the share (see README)" % PYTHON)

    print("\n%s" % ("All good." if not failed else "%d problem(s) to fix." % len(failed)))
    return 1 if failed else 0


def install():
    """Install (or update) the nightly job, the status page and the Quick Action."""
    here = os.path.dirname(os.path.abspath(__file__))
    hour, minute = (int(x) for x in LAUNCH_AT.split(":"))
    # Nightly: if the Mac was asleep, launchd runs it on wake and the run window decides.
    # Polite but not throttled: ProcessType=Background slows ffmpeg badly.
    if not load_agent(LABEL, os.path.join(here, "offair.py"),
                      {"StartCalendarInterval": {"Hour": hour, "Minute": minute},
                       "Nice": 10, "LowPriorityIO": True}):
        return 1
    print("Nightly job installed: runs at %s" % LAUNCH_AT)
    if not load_agent(STATUS_LABEL, os.path.join(here, "offair_status.py"),
                      {"RunAtLoad": True, "KeepAlive": True}):
        return 1
    print("Status page installed: http://localhost:%d" % STATUS_PORT)

    services = os.path.join(HOME, "Library/Services")
    target = os.path.join(services, QUICK_ACTION)
    os.makedirs(services, exist_ok=True)
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(os.path.join(here, QUICK_ACTION), target)
    print("Quick Action installed: right-click a recording in Finder > Quick Actions > Convert now")
    print("Now run: offair.py check")
    return 0


def uninstall():
    for label in (LABEL, STATUS_LABEL):
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), label)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(agent_plist(label)):
            os.remove(agent_plist(label))
    shutil.rmtree(os.path.join(HOME, "Library/Services", QUICK_ACTION), ignore_errors=True)
    print("Removed the nightly job, the status page and the Quick Action. These are untouched:")
    for path in (CONFIG_FILE, os.path.dirname(STATE_FILE), LOG_FILE, WORK_DIR):
        print("  %s" % path)
    return 0


def main():
    p = argparse.ArgumentParser(description="Convert off-air TV recordings to MP4 and file them "
                                            "into media library folders.")
    p.add_argument("command", nargs="?", default="run", choices=("run", "check", "install", "uninstall"))
    p.add_argument("--dry-run", action="store_true", help="list what would be converted and why")
    p.add_argument("--force", action="store_true", help="ignore the run window")
    p.add_argument("--limit", type=int, help="convert at most N files")
    args = p.parse_args()

    problems = load_config()
    if args.command == "check":
        return check(problems)
    if args.command == "uninstall":
        return uninstall()
    setup_logging()
    if problems:
        for problem in problems:
            log.error("Settings: %s", problem)
            print("Settings: %s" % problem, file=sys.stderr)
        return 1
    if args.command == "install":
        return install()

    # launchd stops jobs with SIGTERM; exit through the finally blocks so ffmpeg is
    # killed, the temp file removed and the lock released.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if not acquire_lock():
        log.info("Another run is in progress; exiting")
        return 0
    watch_copies()
    try:
        log.info("--- start%s%s%s", " (dry run)" if args.dry_run else "",
                 " (force)" if args.force else "",
                 " (limit %d)" % args.limit if args.limit is not None else "")
        while True:
            seen = {f for fs in read_requests().values() for f in fs}
            rc = run(args, load_state())
            # A request made while this run was busy found the job already running.
            new = {f for fs in read_requests().values() for f in fs} - seen
            try:
                more_ads = any(not n.startswith(".") for n in os.listdir(AD_REQUESTS_DIR))
            except FileNotFoundError:
                more_ads = False
            if args.dry_run or not (new or more_ads):
                return rc
            log.info("--- new request(s) arrived during the run; going again")
    except Exception as e:
        log.exception("Unexpected error")
        notify("Unexpected error: %s" % e)
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
