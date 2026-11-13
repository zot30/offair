#!/usr/bin/python3
"""offair status page: shows what the nightly job has done and what's waiting or stuck,
with buttons to convert a recording now, stop a run, or queue recordings for Plex's ad
detection.

It serves http://localhost:<port> (see [status] port) to this Mac only. `offair.py
install` sets it up to run all the time; to try it by hand, run this file.

The page never converts anything itself. "Convert now" leaves the same request file as
the Finder Quick Action and starts the nightly job, so the work is done the usual way.
"""

import base64
import datetime
import hmac
import html
import http.server
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import offair as core                                                      # noqa: E402

CACHE_SECONDS = 15                 # how long one look at the share is reused
RECENT = 40                        # converted recordings listed
LOG_LINES = 60


def read_log(lines=400):
    try:
        with open(core.LOG_FILE, errors="replace") as f:
            return f.read().splitlines()[-lines:]
    except OSError:
        return []


def running_pid():
    """The process id of the run in progress, or None."""
    try:
        with open(os.path.join(core.LOCK_DIR, "pid")) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def last_run(log_lines):
    """(started, summary, current) from the log: when the latest real run started, how
    it ended (None if it hasn't), and the recording it last began converting."""
    started = summary = current = None
    for line in log_lines:
        stamp, _, text = line.partition(" INFO    ")
        if text.startswith("--- start") and "(dry run)" not in text:
            started, summary, current = stamp, None, None
        elif started and text.startswith("Converting "):
            current = text[len("Converting "):].split(" (")[0]
        elif started and (text.startswith("Finished: ") or text.endswith("; exiting")
                          or text.startswith("Nothing to convert")):
            summary = text
    return started, summary, current


def when(iso):
    """'today 04:42', 'yesterday 05:10' or 'Tue 10 Nov 05:10' for a timestamp."""
    try:
        t = datetime.datetime.fromisoformat(iso.replace(" ", "T"))
    except (AttributeError, ValueError):
        return iso or ""
    days = (datetime.date.today() - t.date()).days
    day = {0: "today", 1: "yesterday"}.get(days) or t.strftime("%a %d %b").replace(" 0", " ")
    return "%s %s" % (day, t.strftime("%H:%M"))


def snapshot():
    """Everything the page shows, as a dict."""
    problems = core.load_config()
    state = core.load_state() if os.path.exists(core.STATE_FILE) else {"files": {}}
    files = state["files"]
    core.apply_ad_requests(files, core.read_ad_requests())     # show requests the job hasn't read yet
    for _, refile, keys in core.read_ad_requests(core.TIDY_REQUESTS_DIR):
        for key in keys:                                       # asked to be removed: gone already
            if not refile:
                files.pop(key, None)
    for _, add, keys in core.read_ad_requests(core.REDO_REQUESTS_DIR):
        for key in keys:
            if key in files and add:
                files[key].setdefault("redo", "requested")
            elif key in files:
                files[key].pop("redo", None)
    log_lines = read_log()
    started, summary, current = last_run(log_lines)
    snap = {
        "problems": problems, "pid": running_pid(), "started": started, "summary": summary,
        "current": current, "log": log_lines[-LOG_LINES:], "share_down": state.get("share_down"),
        "share": None, "waiting": [], "attention": [], "requests": set(),
    }
    if not problems:
        snap["share"] = core.nas_reachable() and core.share_ok()
    if snap["share"]:
        core.STABLE_CHECK_SECONDS = 0               # this process only looks; no need to wait
        requests = core.read_requests()
        snap["requests"] = set(requests)
        candidates, skipped, _ = core.choose(state, requests)
        for src, size in candidates:
            entry = files.get(core.rel(src), {})
            note = "requested; converting next" if core.request_key(src) in requests else "waiting for the next run"
            if entry.get("redo"):
                note = "converting again; " + note
            if entry.get("failures"):
                note += " (failed %d time(s): %s)" % (entry["failures"], entry.get("last_error", ""))
            snap["waiting"].append((core.rel(src), "%s, %s" % (core.gb(size), note)))
        for src, reason in skipped:
            if reason.startswith("already converted"):
                continue
            target = snap["attention"] if reason.startswith(("gave up", "cannot read", "name differs")) \
                else snap["waiting"]
            target.append((core.rel(src), reason))
    else:
        for key, entry in files.items():            # the share is away: go by what's on record
            if entry.get("status") == "failed":
                snap["attention"].append((key, "failed %d time(s): %s" % (entry.get("failures", 0),
                                                                           entry.get("last_error", ""))))
    for key, entry in sorted(files.items()):
        filed = entry.get("filed", "")
        if entry.get("status") == "done" and filed.startswith("no"):
            mp4 = core.mp4_path(os.path.join(core.SOURCE_DIR, key))
            if not snap["share"] or os.path.exists(mp4):
                snap["attention"].append((key, "converted but not filed: " + filed[4:]))
    done = [(e.get("converted_at", ""), k, e) for k, e in files.items() if e.get("status") == "done"]
    snap["recent"] = sorted(done, reverse=True)
    snap["total"] = len(done)
    snap["known"] = set(files)
    # Converting again: (key, True if its original has to come from the Fetch box first)
    snap["redo"] = sorted((k, bool(snap["share"]) and not os.path.exists(os.path.join(core.SOURCE_DIR, k)))
                          for k, e in files.items() if e.get("redo"))
    snap["queue"] = sorted(((e.get("ads") != "scanning", k, e) for k, e in files.items()
                            if e.get("ads") in ("pending", "scanning")), key=lambda q: q[:2])
    return snap


# ------------------------------------------------------------------- page --

CSS = """
:root { --bg:#f6f6f4; --card:#fff; --ink:#1d1d1f; --dim:#6e6e73; --line:#e3e3e0; --ok:#1a7f37;
        --bad:#c0392b; --warn:#9a6700; --btn:#0b63ce; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#1c1c1e; --card:#2c2c2e; --ink:#f2f2f7; --dim:#a1a1a6; --line:#3a3a3c; --ok:#4cc26a;
          --bad:#ff6b5e; --warn:#e3b341; --btn:#4a9eff; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--ink);
       font:15px/1.45 -apple-system, system-ui, sans-serif; }
main { max-width: 980px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 0 0 10px; }
.dim { color: var(--dim); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px;
        margin-top:16px; overflow-x:auto; }
.pills { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
.pill { border:1px solid var(--line); border-radius:999px; padding:3px 12px; background:var(--card); }
.ok { color: var(--ok); } .bad { color: var(--bad); } .warn { color: var(--warn); }
table { width:100%; border-collapse:collapse; }
td, th { text-align:left; padding:7px 10px 7px 0; border-top:1px solid var(--line); vertical-align:top; }
th { border-top:0; color:var(--dim); font-weight:500; font-size:13px; }
td.act { text-align:right; white-space:nowrap; padding-right:0; }
button { font:inherit; font-size:13px; color:#fff; background:var(--btn); border:0; border-radius:6px;
         padding:4px 12px; cursor:pointer; }
button.stop { background: var(--bad); }
button:disabled { opacity:.45; cursor:default; }
h2 .act { float:right; font-weight:400; font-size:13px; }
input[type=checkbox] { margin:0 8px 0 0; }
a { color: var(--btn); }
button.quiet { background:transparent; color:var(--btn); border:1px solid var(--line); }
.now { display:flex; align-items:center; gap:12px; flex-wrap:wrap; font-size:17px; }
.now .dot { width:10px; height:10px; border-radius:50%; background:var(--dim); flex:none; }
.now.busy .dot { background:var(--ok); animation:pulse 1.6s ease-in-out infinite; }
@keyframes pulse { 50% { opacity:.25; } }
.badge { display:inline-block; border-radius:999px; padding:1px 10px; font-size:12.5px; white-space:nowrap;
         border:1px solid var(--line); color:var(--dim); }
.badge.good { color:var(--ok); border-color:var(--ok); }
.badge.busy { color:#fff; background:var(--btn); border-color:var(--btn); }
.badge.queued { color:var(--warn); border-color:var(--warn); }
.badge.bad { color:var(--bad); border-color:var(--bad); }
.sub { display:block; font-size:12px; color:var(--dim); word-break:break-word; }
.tabs { font-size:13px; font-weight:400; margin-left:8px; }
.tabs b { color:var(--ink); }
details summary { cursor:pointer; font-weight:600; font-size:16px; }
details[open] summary { margin-bottom:10px; }
pre { margin:0; font:12px/1.5 ui-monospace, Menlo, monospace; white-space:pre-wrap; word-break:break-word; }
form { display:inline; margin:0; }
"""


def esc(text):
    return html.escape(str(text))


def name(key):
    """A recording's path as people read it: 'Show Name / S1 E2 - Title'."""
    folder, _, base = key.rpartition(os.sep)
    stem = core.AIR_DATE.sub("", os.path.splitext(base)[0])
    parts = [core.tidy(p) for p in (folder, stem) if p]
    return parts[0] if len(set(parts)) == 1 else " / ".join(parts)     # films: folder = title


REMOVE_WARNING = ("Remove from Offair? Its record goes, along with anything left of it in the recordings "
                  "folder. Nothing in your library is touched, and it won't be downloaded again.")


def button(action, label, key="", css="", confirm=""):
    return ('<form method="post" action="/%s"%s><input type="hidden" name="key" value="%s">'
            '<button class="%s">%s</button></form>'
            % (action, ' onsubmit="return confirm(\'%s\')"' % esc(confirm.replace("'", "\\'")) if confirm else "",
               esc(key), css, esc(label)))


def rows(items, action=None):
    out = []
    for key, note in items:
        act = button("convert", action, key) if action else ""
        out.append('<tr><td>%s</td><td class="dim">%s</td><td class="act">%s</td></tr>'
                   % (esc(name(key)), esc(note), act))
    return "".join(out)


SCRIPT = """
var boxes = document.querySelectorAll('input.pick'), all = document.getElementById('all'),
    go = document.getElementById('go'), redo = document.getElementById('redo'),
    forget = document.getElementById('forget');
function update() {
  var n = document.querySelectorAll('input.pick:checked').length,
      ads = document.querySelectorAll('input.pick:checked:not([data-noads])').length;
  if (go) {
    go.disabled = !ads;
    go.textContent = ads ? 'Detect ads in ' + ads + ' selected' : 'Detect ads in selected';
  }
  redo.disabled = !n;
  redo.textContent = n ? 'Convert ' + n + ' again' : 'Convert again';
  forget.disabled = !n;
  forget.textContent = n ? 'Remove ' + n : 'Remove';
  all.checked = n > 0 && n === boxes.length;
}
if (redo && all) {
  boxes.forEach(function (b) { b.onchange = update; });
  all.onchange = function () { boxes.forEach(function (b) { b.checked = all.checked; }); update(); };
  redo.onclick = function () {
    return confirm('Convert the selected recording(s) again? Each new copy replaces the one in your ' +
                   'library. If the original was deleted, it is downloaded from the Fetch box again first.');
  };
  forget.onclick = function () { return confirm('%s'); };
  update();
}
// Refresh every minute, but not while you're part-way through choosing.
setInterval(function () { if (!document.querySelector('input.pick:checked')) location.reload(); }, 60000);
"""


def ad_badge(entry):
    """The coloured label for a recording's ad breaks."""
    ads = entry.get("ads")
    if ads == "scanning":
        return '<span class="badge busy">Scanning now</span>'
    if ads == "pending":
        return '<span class="badge queued">Queued</span>'
    if ads == "none found":
        return '<span class="badge">No ads found</span>'
    if ads:
        return '<span class="badge good">%s</span>' % esc(ads.replace("(s)", "s").replace("1 ad breaks", "1 ad break"))
    if "ad_breaks" in entry:
        return '<span class="badge good">%d chapter break(s)</span>' % entry["ad_breaks"]
    return '<span class="badge">Not scanned</span>'


def filed_ok(entry):
    return bool(entry.get("filed")) and not entry["filed"].startswith("no")


VIEWS = (("", "latest %d" % RECENT), ("all", "all"), ("unscanned", "not scanned"))


def page(snap, view=""):
    plex = core.PLEX_ADS and not snap["problems"]
    scanning = [(k, e) for first, k, e in snap["queue"] if not first]
    queued = [(k, e) for first, k, e in snap["queue"] if first]

    # What is happening right now, in one line.
    if snap["problems"]:
        now, busy = "Settings need fixing", False
    elif snap["pid"] and scanning:
        now, busy = "Plex is scanning %s for ads (since %s)" % (
            name(scanning[0][0]), when(scanning[0][1].get("ads_since")).replace("today ", "")), True
    elif snap["pid"]:
        now, busy = ("Converting %s" % name(snap["current"]) if snap["current"] else "Working"), True
    else:
        now, busy = "Idle. Next run at %s" % core.LAUNCH_AT, False
    if busy and queued:
        now += ", %d more queued for ads" % len(queued)
    if snap["started"]:
        last = "Last run started %s" % when(snap["started"])
        if snap["summary"] and not snap["pid"]:
            last += ": " + snap["summary"][0].lower() + snap["summary"][1:]
    else:
        last = "No runs in the log yet"
    pills = []
    if snap["share"] is not None:
        down = snap["share_down"] or {}
        pills.append('<span class="pill ok">Recordings folder reachable</span>' if snap["share"] else
                     '<span class="pill bad">Recordings folder unreachable%s</span>'
                     % (" (%d nights)" % down["nights"] if down.get("nights") else ""))
    pills.append('<span class="pill">Run window %s–%s</span>' % (core.RUN_START, core.RUN_END))
    pills.append('<span class="pill">Originals %s</span>' % ("kept" if core.KEEP_ORIGINALS else "deleted after converting"))
    body = ['<h1>Offair</h1><div class="card"><div class="now%s"><span class="dot"></span><span>%s</span>%s</div>'
            '<div class="dim" style="margin-top:6px">%s. %d recording(s) converted so far.</div>'
            '<div class="pills">%s</div></div>'
            % (" busy" if busy else "", esc(now),
               button("stop", "Stop this run", css="stop") if snap["pid"] else "",
               esc(last), snap["total"], "".join(pills))]

    if snap["problems"]:
        body.append('<div class="card"><h2 class="bad">Settings (%s)</h2><pre>%s</pre></div>'
                    % (esc(core.CONFIG_FILE), esc("\n".join(snap["problems"]))))
    if snap["attention"]:
        body.append('<div class="card"><h2 class="warn">Needs attention (%d)</h2><table>%s</table></div>'
                    % (len(snap["attention"]), "".join(
                        '<tr><td>%s</td><td class="dim">%s</td><td class="act">%s</td></tr>'
                        % (esc(name(k)), esc(note),
                           (button("convert", "Try again", k) + " " if note.startswith(("gave up", "failed")) and snap["share"]
                            else button("refile", "File it now", k) + " " if note.startswith("converted but not filed") and snap["share"]
                            else "") + (button("forget", "Remove", k, css="quiet", confirm=REMOVE_WARNING)
                                        if k in snap["known"] else ""))
                        for k, note in snap["attention"])))

    # The ad queue: what Plex is scanning and what's lined up, each removable.
    if plex and snap["queue"]:
        lines = "".join(
            '<tr><td>%s</td><td>%s</td><td class="act">%s</td></tr>'
            % (esc(name(k)), ad_badge(e),
               '<span class="dim">can\'t be interrupted</span>' if e["ads"] == "scanning"
               else button("ads/remove", "Remove", k, css="quiet"))
            for _, k, e in snap["queue"])
        clear = "" if len(queued) < 2 else (
            '<span class="act"><form method="post" action="/ads/remove">%s<button class="quiet">'
            'Remove all %d queued</button></form></span>'
            % ("".join('<input type="hidden" name="key" value="%s">' % esc(k) for k, _ in queued), len(queued)))
        body.append('<div class="card"><h2>Ad detection queue (%d)%s</h2><table>%s</table>'
                    '<div class="dim" style="margin-top:8px;font-size:13px">Plex scans one recording at a '
                    'time. Anything still queued when the run window opens waits for the nightly run.</div></div>'
                    % (len(snap["queue"]), clear, lines))

    if snap["redo"]:
        body.append('<div class="card"><h2>Converting again (%d)</h2><table>%s</table>'
                    '<div class="dim" style="margin-top:8px;font-size:13px">The new copy replaces the one in '
                    'your library once it has passed the checks. Until then the old one stays.</div></div>'
                    % (len(snap["redo"]), "".join(
                        '<tr><td>%s</td><td><span class="badge queued">%s</span></td><td class="act">%s</td></tr>'
                        % (esc(name(k)), "Waiting for the Fetch download" if fetch else "Waiting to be converted",
                           "" if snap["pid"] and snap["current"] == k else button("redo/remove", "Cancel", k, css="quiet"))
                        for k, fetch in snap["redo"])))
    if snap["share"]:
        waiting = snap["waiting"]
        body.append('<div class="card"><h2>Waiting to be converted (%d)</h2>%s</div>' % (len(waiting), (
            "<table>%s</table>" % "".join(
                '<tr><td>%s</td><td class="dim">%s</td><td class="act">%s</td></tr>'
                % (esc(name(k)), esc(n), '<span class="badge queued">Next</span>'
                   if k in snap["requests"] or "requested" in n
                   else button("convert", "Convert now", k)) for k, n in waiting))
            if waiting else '<span class="dim">Nothing is waiting to be converted.</span>'))

    # Converted recordings, with their place in the library and their ad breaks.
    everything = snap["recent"]
    listed = {"": everything[:RECENT], "all": everything}.get(
        view, [r for r in everything if filed_ok(r[2]) and "ads" not in r[2]])

    def pick(k, e):
        if e.get("redo"):
            return ""
        return '<input type="checkbox" class="pick" form="ads" name="key" value="%s"%s>' % (
            esc(k), "" if plex and filed_ok(e) and e.get("ads") not in ("pending", "scanning") else " data-noads")

    def place(e):
        if filed_ok(e):
            return '<span class="sub">%s</span>' % esc(e["filed"])
        return '<span class="sub">%s</span>' % ("not filed: already in the library" if e.get("filed")
                                                else "beside the recording")
    recent = "".join(
        '<tr><td><label>%s%s</label>%s</td><td class="dim" style="white-space:nowrap">%s</td>%s</tr>'
        % (pick(k, e), esc(name(k)), place(e), esc(when(e.get("converted_at"))),
           '<td class="act">%s</td>' % ('<span class="badge queued">Converting again</span>' if e.get("redo")
                                        else ad_badge(e) if filed_ok(e) else "") if plex else "")
        for _, k, e in listed)
    tabs = " · ".join("<b>%s</b>" % label if v == view else '<a href="/%s">%s</a>' % ("?show=" + v if v else "", label)
                      for v, label in VIEWS if plex or v != "unscanned")
    body.append('<div class="card"><h2>Converted <span class="tabs">%s</span>%s</h2>%s</div>'
                % (tabs,
                   '<span class="act"><form id="ads" method="post" action="/ads">%s<button id="redo" '
                   'class="quiet" formaction="/redo" disabled>Convert again</button> <button id="forget" '
                   'class="quiet" formaction="/forget" disabled>Remove</button></form></span>'
                   % ('<button id="go" disabled>Detect ads in selected</button> ' if plex else ""),
                   '<table><tr><th>%sRecording</th><th>Converted</th>%s</tr>%s</table>'
                   % ('<input type="checkbox" id="all" title="Select all">',
                      '<th style="text-align:right">Ads</th>' if plex else "", recent)
                   if listed else '<span class="dim">Nothing to show here.</span>'))
    body.append('<div class="card"><details><summary>Log</summary><pre>%s</pre></details></div>'
                % esc("\n".join(snap["log"])))
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Offair</title>'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<style>%s</style></head><body><main>%s</main><script>%s</script>'
            '</body></html>' % (CSS, "".join(body), SCRIPT % REMOVE_WARNING.replace("'", "\\'")))


# ---------------------------------------------------------------- actions --

def request_convert(key):
    """Queue a Convert-now request for the recording at key (relative to the recordings
    folder) and start the job. Returns False if key isn't a recording there."""
    path = os.path.realpath(os.path.join(core.SOURCE_DIR, key))
    inside = path.startswith(os.path.realpath(core.SOURCE_DIR) + os.sep)
    if not (inside and os.path.isfile(path) and path.lower().endswith(core.SOURCE_EXTENSIONS)):
        return False
    os.makedirs(core.REQUESTS_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".new.", dir=core.REQUESTS_DIR)
    with os.fdopen(fd, "w") as f:
        f.write(path + "\n")
    os.rename(tmp, os.path.join(core.REQUESTS_DIR, os.path.basename(tmp)[len(".new."):]))
    launchctl("kickstart")
    return True


def request_ads(keys, remove=False):
    """Queue converted recordings (state keys) for Plex's ad detection, or take queued
    ones off again, and start the job. Returns False if none of them is a filed recording."""
    files = core.load_state()["files"]
    keys = [k for k in keys if filed_ok(files.get(k, {}))]
    if not keys:
        return False
    os.makedirs(core.AD_REQUESTS_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".new.", dir=core.AD_REQUESTS_DIR)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(keys) + "\n")
    # Named by the time, so the job applies requests in the order they were made.
    os.rename(tmp, os.path.join(core.AD_REQUESTS_DIR, "%.6f.%s" % (time.time(), "remove" if remove else "add")))
    launchctl("kickstart")
    return True


def request_redo(keys, remove=False):
    """Ask for converted recordings (state keys) to be converted again, or cancel that,
    and start the job. Returns False if none of them has been converted."""
    files = core.load_state()["files"]
    keys = [k for k in keys if files.get(k, {}).get("status") == "done"]
    if not keys:
        return False
    os.makedirs(core.REDO_REQUESTS_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".new.", dir=core.REDO_REQUESTS_DIR)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(keys) + "\n")
    os.rename(tmp, os.path.join(core.REDO_REQUESTS_DIR, "%.6f.%s" % (time.time(), "remove" if remove else "add")))
    # A convert-now request for each, so the job acts on it straight away, like the Quick Action.
    os.makedirs(core.REQUESTS_DIR, exist_ok=True)
    for i, key in enumerate([] if remove else keys):
        with open(os.path.join(core.REQUESTS_DIR, "redo-%.6f-%d" % (time.time(), i)), "w") as f:
            f.write(os.path.join(core.SOURCE_DIR, key) + "\n")
    launchctl("kickstart")
    return True


def request_tidy(keys, refile=False):
    """Ask for recordings (state keys) to be removed from offair's records, or for an
    unfiled MP4 to be filed again, and start the job. Returns False if none is known."""
    files = core.load_state()["files"]
    keys = [k for k in keys if k in files]
    if not keys:
        return False
    os.makedirs(core.TIDY_REQUESTS_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".new.", dir=core.TIDY_REQUESTS_DIR)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(keys) + "\n")
    os.rename(tmp, os.path.join(core.TIDY_REQUESTS_DIR, "%.6f.%s" % (time.time(), "add" if refile else "remove")))
    launchctl("kickstart")
    return True


def launchctl(*args):
    target = "gui/%d/%s" % (os.getuid(), core.LABEL)
    subprocess.run(["launchctl"] + list(args) + [target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Handler(http.server.BaseHTTPRequestHandler):
    cache = (0.0, None)

    def local(self):
        """Who may use the page. This Mac's own browser, at this page's own address, always:
        that refuses other sites' forms and addresses that merely point here. With
        [status] on_network, anything else is let in with the password."""
        hosts = {"localhost:%d" % core.STATUS_PORT, "127.0.0.1:%d" % core.STATUS_PORT}
        host, origin = self.headers.get("Host"), self.headers.get("Origin")
        if origin is not None and origin != "http://%s" % host:
            return False                            # a form on some other site
        if self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1") and host in hosts:
            return True
        if not (core.STATUS_ON_NETWORK and core.STATUS_PASSWORD):
            return False
        try:
            kind, _, given = (self.headers.get("Authorization") or "").partition(" ")
            password = base64.b64decode(given).decode("utf-8", "replace").partition(":")[2]
        except ValueError:
            password = ""
        if kind == "Basic" and hmac.compare_digest(password.encode(), core.STATUS_PASSWORD.encode()):
            return True
        self.ask_password = True
        return False

    def refuse(self):
        if getattr(self, "ask_password", False):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Offair", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send(403, "Forbidden")

    def send(self, code, text="", location=None):
        data = text.encode()
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.local():
            return self.refuse()
        views = {"/" + ("?show=" + v if v else ""): v for v, _ in VIEWS}
        if self.path not in views:
            return self.send(404, "Not found")
        at, snap = Handler.cache
        if snap is None or time.time() - at > CACHE_SECONDS:
            snap = snapshot()
            Handler.cache = (time.time(), snap)
        self.send(200, page(snap, views[self.path]))

    def do_POST(self):
        if not self.local():
            return self.refuse()
        length = min(int(self.headers.get("Content-Length") or 0), 10000)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        if self.path == "/convert":
            core.load_config()
            if not request_convert(form.get("key", [""])[0]):
                return self.send(400, "That isn't a recording in the recordings folder.")
        elif self.path in ("/ads", "/ads/remove"):
            core.load_config()
            if not (core.PLEX_ADS and request_ads(form.get("key", []), remove=self.path != "/ads")):
                return self.send(400, "None of those is a filed recording, or [plex] detect_ads is off.")
        elif self.path in ("/redo", "/redo/remove"):
            core.load_config()
            if not request_redo(form.get("key", []), remove=self.path != "/redo"):
                return self.send(400, "None of those is a converted recording.")
        elif self.path in ("/forget", "/refile"):
            core.load_config()
            if not request_tidy(form.get("key", []), refile=self.path == "/refile"):
                return self.send(400, "Offair has no record of that.")
        elif self.path == "/stop":
            launchctl("kill", "TERM")
        else:
            return self.send(404, "Not found")
        time.sleep(1)                               # let the job start or stop before the page reloads
        Handler.cache = (0.0, None)
        self.send(303, location="/")

    def log_message(self, *args):
        pass


def main():
    core.load_config()
    if core.STATUS_ON_NETWORK and core.STATUS_PASSWORD:
        class Server(http.server.ThreadingHTTPServer):         # IPv4 and IPv6, every interface
            address_family = socket.AF_INET6

            def server_bind(self):
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                super().server_bind()
        server = Server(("::", core.STATUS_PORT), Handler)
    else:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", core.STATUS_PORT), Handler)
    print("offair status page: http://localhost:%d" % core.STATUS_PORT, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
