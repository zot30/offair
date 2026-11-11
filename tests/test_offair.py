"""Tests for offair. Run from the project folder with:

    /usr/bin/python3 -m unittest discover tests -v

Everything happens in temporary folders; your settings, recordings and libraries are
not touched. The end-to-end test needs ffmpeg and takes a few seconds.
"""

import argparse
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    """A fresh copy of the script with every folder pointed into a temporary directory."""
    spec = importlib.util.spec_from_file_location("offair", os.path.join(HERE, "..", "offair.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    # realpath: macOS temp folders sit behind a symlink (/var -> /private/var)
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="offair-test-"))
    m.SOURCE_DIR = os.path.join(tmp, "Recordings")
    m.TV_DIR = os.path.join(tmp, "TV")
    m.MOVIES_DIR = os.path.join(tmp, "Movies")
    m.WORK_DIR = os.path.join(tmp, "work")
    m.STATE_FILE = os.path.join(tmp, "state", "state.json")
    m.REQUESTS_DIR = os.path.join(tmp, "state", "requests")
    m.LOG_FILE = os.path.join(tmp, "offair.log")
    m.LOCK_DIR = os.path.join(m.WORK_DIR, "run.lock")
    m.FFMPEG_LOG = os.path.join(m.WORK_DIR, "ffmpeg-last.log")
    for d in (m.SOURCE_DIR, m.TV_DIR, m.MOVIES_DIR, m.WORK_DIR):
        os.makedirs(d)
    m.notifications = []
    m.notify = m.notifications.append          # no real notifications
    m.on_mains_power = lambda: True
    return m, tmp


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.m, self.tmp = load()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class Config(Base):
    def write(self, text):
        path = os.path.join(self.tmp, "config.ini")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_values_are_applied(self):
        problems = self.m.load_config(self.write(
            "[folders]\nrecordings = /r/\ntv = /t\nmovies = ~/m\nextensions = .mpeg, .ts\n"
            "[encode]\ncrf = 18\n[files]\nkeep_originals = no\n[schedule]\nlaunch_at = 03:15\n"))
        self.assertEqual(problems, [])
        self.assertEqual(self.m.SOURCE_DIR, "/r")
        self.assertEqual(self.m.MOVIES_DIR, os.path.expanduser("~/m"))
        self.assertEqual(self.m.SOURCE_EXTENSIONS, (".mpeg", ".ts"))
        self.assertEqual(self.m.CRF, 18)
        self.assertIs(self.m.KEEP_ORIGINALS, False)
        self.assertEqual(self.m.LAUNCH_AT, "03:15")

    def test_mistakes_are_reported(self):
        self.m.SOURCE_DIR = ""
        problems = self.m.load_config(self.write(
            "[folders]\nrecordngs = /r\n[encode]\ncrf = high\n[files]\nkeep_originals = maybe\n"
            "[schedule]\nlaunch_at = 4.30\n"))
        text = "\n".join(problems)
        for expected in ("recordngs is not a setting", "crf must be a whole number",
                         "keep_originals must be yes or no", "launch_at must be a 24-hour time",
                         "recordings is not set"):
            self.assertIn(expected, text)

    def test_missing_file(self):
        self.assertIn("no config file", self.m.load_config(os.path.join(self.tmp, "nope.ini"))[0])

    def test_example_config_is_valid(self):
        self.assertEqual(self.m.load_config(os.path.join(HERE, "..", "config.example.ini")), [])


class Filing(Base):
    def test_episode(self):
        src = os.path.join(self.m.SOURCE_DIR, "Would_I_Lie_to_You", "S16_E10_-_Episode_10_-_Wed_07_Jan.mpeg")
        target, already = self.m.library_target(src)
        self.assertEqual(target, os.path.join(self.m.TV_DIR, "Would I Lie to You", "Season 16",
                                              "Would I Lie to You - S16E10 - Episode 10.mp4"))
        self.assertIsNone(already)

    def test_existing_show_folder_is_reused_ignoring_case(self):
        os.makedirs(os.path.join(self.m.TV_DIR, "Would I Lie To You"))
        src = os.path.join(self.m.SOURCE_DIR, "Would_I_Lie_to_You", "S1_E2_-_Mon_09_Nov.mpeg")
        target, _ = self.m.library_target(src)
        self.assertEqual(target, os.path.join(self.m.TV_DIR, "Would I Lie To You", "Season 1",
                                              "Would I Lie To You - S01E02.mp4"))

    def test_episode_already_in_library(self):
        have = touch(os.path.join(self.m.TV_DIR, "Show", "Specials", "show.s01e02.720p.mkv"))
        touch(os.path.join(self.m.TV_DIR, "Show", "Season 1", "Show - S01E20.mp4"))   # not a match
        _, already = self.m.library_target(os.path.join(self.m.SOURCE_DIR, "Show", "S1_E2_-_Title_-_Mon_09_Nov.mpeg"))
        self.assertEqual(already, have)

    def test_film(self):
        src = os.path.join(self.m.SOURCE_DIR, "Robocop", "Robocop_-_Sat_07_Nov.mpeg")
        target, already = self.m.library_target(src)
        self.assertEqual(target, os.path.join(self.m.MOVIES_DIR, "Robocop", "Robocop.mp4"))
        self.assertIsNone(already)
        os.makedirs(os.path.join(self.m.MOVIES_DIR, "robocop"))
        self.assertEqual(self.m.library_target(src)[1], os.path.join(self.m.MOVIES_DIR, "robocop"))


class Choosing(Base):
    def test_case_clashes(self):
        self.assertEqual(self.m.case_clashes(["Show_A", "show_a", "Other"]), {"Show_A", "show_a"})

    def test_reasons_for_skipping(self):
        m = self.m
        m.MIN_SIZE_MB, m.STABLE_CHECK_SECONDS = 0, 0
        old = touch(os.path.join(m.SOURCE_DIR, "Show", "S1_E1_-_Old.mpeg"))
        os.utime(old, (0, 0))
        fresh = touch(os.path.join(m.SOURCE_DIR, "Show", "S1_E2_-_Fresh.mpeg"))
        done = touch(os.path.join(m.SOURCE_DIR, "Show", "S1_E3_-_Done.mpeg"))
        os.utime(done, (0, 0))
        touch(os.path.join(m.SOURCE_DIR, "Show", "notes.txt"))
        state = {"files": {m.rel(done): {"status": "done", "filed": "TV/Show/x.mp4"}}}
        candidates, skipped, clashes = m.choose(state)
        self.assertEqual([src for src, _ in candidates], [old])
        reasons = dict(skipped)
        self.assertIn("still downloading", reasons[fresh])
        self.assertIn("already converted and filed", reasons[done])
        self.assertEqual(clashes, [])
        # A Convert-now request skips the age check and goes first.
        candidates, _, _ = m.choose(state, requests={m.request_key(fresh): []})
        self.assertEqual([src for src, _ in candidates], [fresh, old])

    def test_window(self):
        m = self.m
        now = __import__("datetime").datetime.now()
        m.RUN_START, m.RUN_END = "00:00", "23:59"
        self.assertTrue(m.in_window() or now.strftime("%H:%M") == "23:59")
        m.RUN_START = m.RUN_END = now.strftime("%H:%M")
        self.assertFalse(m.in_window())


class Chapters(Base):
    def test_ads_and_parts(self):
        path = os.path.join(self.tmp, "chapters.txt")
        self.m.write_chapters([(300.0, 480.0), (900.0, 1000.0)], 1000.0, path)
        with open(path) as f:
            text = f.read()
        titles = [line[6:] for line in text.splitlines() if line.startswith("title=")]
        self.assertEqual(titles, ["Part 1", "Ads", "Part 2", "Ads"])
        self.assertIn("START=300000\nEND=480000\ntitle=Ads", text)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class EndToEnd(Base):
    def record(self, relpath, seconds=4):
        """Make a small interlaced MPEG-TS recording, like a set-top box saves."""
        path = os.path.join(self.m.SOURCE_DIR, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=%d:s=720x576:r=25" % seconds,
                        "-f", "lavfi", "-i", "sine=d=%d" % seconds, "-c:v", "mpeg2video", "-flags", "+ildct+ilme",
                        "-c:a", "ac3", "-f", "mpegts", path], check=True)
        return path

    def test_convert_and_file(self):
        m = self.m
        m.MIN_SIZE_MB, m.MIN_AGE_MINUTES, m.STABLE_CHECK_SECONDS = 0, 0, 0
        episode = self.record("Test_Show/S2_E5_-_The_One_With_Bars_-_Mon_09_Nov.mpeg")
        film = self.record("Test_Film/Test_Film_-_Sat_07_Nov.mpeg")
        m.setup_logging()
        args = argparse.Namespace(dry_run=False, force=True, limit=None)
        self.assertEqual(m.run(args, m.load_state()), 0)

        tv = os.path.join(m.TV_DIR, "Test Show", "Season 2", "Test Show - S02E05 - The One With Bars.mp4")
        movie = os.path.join(m.MOVIES_DIR, "Test Film", "Test Film.mp4")
        for out in (tv, movie):
            self.assertTrue(os.path.exists(out), out)
            duration, streams = m.probe(out)
            self.assertAlmostEqual(duration, 4, delta=0.5)
            self.assertEqual(sorted(s["codec_name"] for s in streams), ["aac", "h264"])
        self.assertTrue(os.path.exists(episode) and os.path.exists(film))       # originals kept
        state = m.load_state()["files"]
        self.assertEqual(state[m.rel(episode)]["status"], "done")
        self.assertEqual(os.listdir(m.WORK_DIR), ["ffmpeg-last.log"])           # nothing left behind

        # A second run converts nothing and overwrites nothing.
        before = os.path.getmtime(tv)
        self.assertEqual(m.run(args, m.load_state()), 0)
        self.assertEqual(os.path.getmtime(tv), before)

    def test_originals_deleted_when_asked(self):
        m = self.m
        m.MIN_SIZE_MB, m.MIN_AGE_MINUTES, m.STABLE_CHECK_SECONDS = 0, 0, 0
        m.KEEP_ORIGINALS = False
        src = self.record("Test_Show/S1_E1_-_Pilot_-_Mon_09_Nov.mpeg")
        m.setup_logging()
        m.run(argparse.Namespace(dry_run=False, force=True, limit=None), m.load_state())
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.exists(os.path.join(m.TV_DIR, "Test Show", "Season 1", "Test Show - S01E01 - Pilot.mp4")))

    def test_broken_recording_fails_without_filing(self):
        m = self.m
        m.MIN_SIZE_MB, m.MIN_AGE_MINUTES, m.STABLE_CHECK_SECONDS = 0, 0, 0
        bad = os.path.join(m.SOURCE_DIR, "Test_Show", "S1_E9_-_Broken.mpeg")
        os.makedirs(os.path.dirname(bad))
        with open(bad, "wb") as f:
            f.write(b"not a recording" * 1000)
        m.setup_logging()
        m.run(argparse.Namespace(dry_run=False, force=True, limit=None), m.load_state())
        entry = m.load_state()["files"][m.rel(bad)]
        self.assertEqual((entry["status"], entry["failures"]), ("failed", 1))
        self.assertTrue(os.path.exists(bad))
        self.assertEqual(os.listdir(m.TV_DIR), [])


class StalledCopy(Base):
    def test_copy_file_copies(self):
        src = os.path.join(self.tmp, "a")
        with open(src, "wb") as f:
            f.write(b"x" * 100000)
        self.m.copy_file(src, src + ".copy")
        self.assertEqual(os.path.getsize(src + ".copy"), 100000)
        self.assertIsNone(self.m._copying["what"])

    def test_watchdog_fires_only_when_a_copy_stops_moving(self):
        import time
        m, fired = self.m, []
        m.copy_stalled = fired.append
        m.COPY_STALL_MINUTES = 1
        m.watch_copies(interval=0.05)
        m._copying.update(what="slow.mpeg", at=time.time())        # copying, making progress
        time.sleep(0.2)
        self.assertEqual(fired, [])
        m._copying.update(at=time.time() - 120)                    # no progress for 2 minutes
        time.sleep(0.2)
        self.assertEqual(fired[0], "slow.mpeg")
        m._copying["what"] = None


class StatusPage(Base):
    def setUp(self):
        super().setUp()
        import sys
        sys.path.insert(0, os.path.join(HERE, ".."))
        import offair_status
        self.s = offair_status
        self.s.core = self.m
        self.m.load_config = lambda path=None: []                  # keep the temp folders
        self.m.MIN_SIZE_MB = 0
        self.kicks = []
        self.s.launchctl = lambda *a: self.kicks.append(a)

    def test_page_lists_waiting_failed_and_converted(self):
        m = self.m
        waiting = touch(os.path.join(m.SOURCE_DIR, "Show", "S1_E1_-_Waiting_-_Mon_09_Nov.mpeg"))
        os.utime(waiting, (0, 0))
        gave_up = touch(os.path.join(m.SOURCE_DIR, "Show", "S1_E2_-_Broken.mpeg"))
        os.utime(gave_up, (0, 0))
        done = touch(os.path.join(m.SOURCE_DIR, "Show", "S1_E3_-_Done.mpeg"))
        m.save_state({"files": {
            m.rel(gave_up): {"status": "failed", "failures": 3, "last_error": "no <video> stream"},
            m.rel(done): {"status": "done", "converted_at": "2026-11-10T05:10:00",
                          "filed": "TV/Show/Season 1/Show - S01E03 - Done.mp4"}}})
        snap = self.s.snapshot()
        self.assertEqual([k for k, _ in snap["waiting"]], [m.rel(waiting)])
        self.assertEqual([k for k, _ in snap["attention"]], [m.rel(gave_up)])
        text = self.s.page(snap)
        for expected in ("Show / S1 E1 - Waiting", "Convert now", "Try again", "no &lt;video&gt; stream",
                         "Show - S01E03 - Done.mp4", "Idle"):
            self.assertIn(expected, text)
        self.assertNotIn("<video>", text)

    def test_convert_request_only_for_real_recordings(self):
        m = self.m
        src = touch(os.path.join(m.SOURCE_DIR, "Show", "S1_E1_-_Pilot.mpeg"))
        touch(os.path.join(self.tmp, "outside.mpeg"))
        self.assertFalse(self.s.request_convert("../outside.mpeg"))
        self.assertFalse(self.s.request_convert("Show/missing.mpeg"))
        self.assertEqual(self.kicks, [])
        self.assertTrue(self.s.request_convert(m.rel(src)))
        self.assertEqual(self.kicks, [("kickstart",)])
        self.assertEqual(list(m.read_requests()), [m.request_key(src)])


if __name__ == "__main__":
    unittest.main()
