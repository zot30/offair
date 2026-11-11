#!/usr/bin/python3
"""offair status page: shows what the nightly job has done and what's waiting or stuck,
with buttons to convert a recording now or stop a run.

It serves http://localhost:<port> (see [status] port) to this Mac only. `offair.py
install` sets it up to run all the time; to try it by hand, run this file.

The page never converts anything itself. "Convert now" leaves the same request file as
the Finder Quick Action and starts the nightly job, so the work is done the usual way.
"""

import datetime
import html
import http.server
import os
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
    snap["recent"] = sorted(done, reverse=True)[:RECENT]
    snap["total"] = len(done)
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


def button(action, label, key="", css=""):
    return ('<form method="post" action="/%s"><input type="hidden" name="key" value="%s">'
            '<button class="%s">%s</button></form>' % (action, esc(key), css, esc(label)))


def rows(items, action=None):
    out = []
    for key, note in items:
        act = button("convert", action, key) if action else ""
        out.append('<tr><td>%s</td><td class="dim">%s</td><td class="act">%s</td></tr>'
                   % (esc(name(key)), esc(note), act))
    return "".join(out)


def page(snap):
    pills = []
    if snap["problems"]:
        pills.append('<span class="pill bad">Settings need fixing</span>')
    elif snap["pid"]:
        pills.append('<span class="pill ok">Running: %s</span>' % esc(name(snap["current"]) if snap["current"]
                                                                       else "starting"))
    else:
        pills.append('<span class="pill">Idle</span>')
    if snap["share"] is not None:
        down = snap["share_down"] or {}
        pills.append('<span class="pill ok">Recordings folder reachable</span>' if snap["share"] else
                     '<span class="pill bad">Recordings folder unreachable%s</span>'
                     % (" (%d nights)" % down["nights"] if down.get("nights") else ""))
    pills.append('<span class="pill">Next run %s, window %s–%s</span>'
                 % (core.LAUNCH_AT, core.RUN_START, core.RUN_END))
    pills.append('<span class="pill">Originals %s</span>' % ("kept" if core.KEEP_ORIGINALS else "deleted after converting"))
    if snap["started"]:
        last = "Last run started %s" % when(snap["started"])
        if snap["summary"] and not snap["pid"]:
            last += ": " + snap["summary"][0].lower() + snap["summary"][1:]
    else:
        last = "No runs in the log yet"

    body = ['<h1>Offair</h1><div class="dim">%s. %d recording(s) converted so far.</div>'
            % (esc(last), snap["total"]), '<div class="pills">%s%s</div>'
            % ("".join(pills), " " + button("stop", "Stop this run", css="stop") if snap["pid"] else "")]
    if snap["problems"]:
        body.append('<div class="card"><h2 class="bad">Settings (%s)</h2><pre>%s</pre></div>'
                    % (esc(core.CONFIG_FILE), esc("\n".join(snap["problems"]))))
    if snap["attention"]:
        body.append('<div class="card"><h2 class="warn">Needs attention (%d)</h2><table>%s</table></div>'
                    % (len(snap["attention"]), "".join(
                        '<tr><td>%s</td><td class="dim">%s</td><td class="act">%s</td></tr>'
                        % (esc(name(k)), esc(note),
                           button("convert", "Try again", k) if note.startswith(("gave up", "failed")) and snap["share"] else "")
                        for k, note in snap["attention"])))
    if snap["share"]:
        waiting = [(k, n) for k, n in snap["waiting"]]
        body.append('<div class="card"><h2>Waiting (%d)</h2>%s</div>' % (len(waiting), (
            "<table>%s</table>" % "".join(
                '<tr><td>%s</td><td class="dim">%s</td><td class="act">%s</td></tr>'
                % (esc(name(k)), esc(n), "" if k in snap["requests"] or "requested" in n
                   else button("convert", "Convert now", k)) for k, n in waiting))
            if waiting else '<span class="dim">Nothing is waiting to be converted.</span>'))
    recent = "".join(
        '<tr><td>%s</td><td>%s</td><td class="dim">%s</td><td class="dim">%s</td></tr>'
        % (esc(name(k)), esc(when(e.get("converted_at"))),
           esc(e["filed"]) if e.get("filed") and not e["filed"].startswith("no") else
           ("not filed" if e.get("filed") else "beside the recording"),
           "waiting for Plex to scan for ads" if e.get("ads") == "pending" else esc(e["ads"])
           if "ads" in e else "" if "ad_breaks" not in e else "%d ad break(s)" % e["ad_breaks"])
        for _, k, e in snap["recent"])
    body.append('<div class="card"><h2>Converted (latest %d of %d)</h2><table><tr><th>Recording</th>'
                '<th>Converted</th><th>Filed as</th><th>Ads</th></tr>%s</table></div>'
                % (len(snap["recent"]), snap["total"], recent))
    body.append('<div class="card"><h2>Log</h2><pre>%s</pre></div>' % esc("\n".join(snap["log"])))
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Offair</title>'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="refresh" content="60"><style>%s</style></head><body><main>%s</main>'
            '</body></html>' % (CSS, "".join(body)))


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


def launchctl(*args):
    target = "gui/%d/%s" % (os.getuid(), core.LABEL)
    subprocess.run(["launchctl"] + list(args) + [target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Handler(http.server.BaseHTTPRequestHandler):
    cache = (0.0, "")

    def local(self):
        """Only this Mac's own browser, on this page: refuses other sites' forms and
        addresses that merely point here."""
        hosts = {"localhost:%d" % core.STATUS_PORT, "127.0.0.1:%d" % core.STATUS_PORT}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in hosts and (origin is None or origin in {"http://" + h for h in hosts})

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
            return self.send(403, "Forbidden")
        if self.path != "/":
            return self.send(404, "Not found")
        at, text = Handler.cache
        if time.time() - at > CACHE_SECONDS:
            text = page(snapshot())
            Handler.cache = (time.time(), text)
        self.send(200, text)

    def do_POST(self):
        if not self.local():
            return self.send(403, "Forbidden")
        length = min(int(self.headers.get("Content-Length") or 0), 10000)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        if self.path == "/convert":
            core.load_config()
            if not request_convert(form.get("key", [""])[0]):
                return self.send(400, "That isn't a recording in the recordings folder.")
        elif self.path == "/stop":
            launchctl("kill", "TERM")
        else:
            return self.send(404, "Not found")
        time.sleep(1)                               # let the job start or stop before the page reloads
        Handler.cache = (0.0, "")
        self.send(303, location="/")

    def log_message(self, *args):
        pass


def main():
    core.load_config()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", core.STATUS_PORT), Handler)
    print("offair status page: http://localhost:%d" % core.STATUS_PORT, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
