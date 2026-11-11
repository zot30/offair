# offair

Every night, offair converts the TV recordings in a folder into MP4 files that a media
server can play directly, and files them into your TV and film library folders with
names the server recognises.

It was written for recordings downloaded from a Fetch TV box with
[fetchtv-cli](https://pypi.org/project/fetchtv-cli) and watched in Plex, on a Mac that
converts and a NAS that stores. It works with any folder of MPEG-TS recordings named
the same way, on local disks or a network share.

offair is an independent project. It is not affiliated with or endorsed by Fetch TV or
Plex.

- **Runs unattended.** A LaunchAgent starts it at 04:30. It works only on mains power,
  inside a time window, and when the share is reachable.
- **Checks before it trusts.** Each MP4 must have one video and one audio stream and
  match the recording's length before it replaces anything.
- **Never overwrites.** An episode or film already in your library is left alone.
- **Keeps originals** until you tell it otherwise.
- **No dependencies** beyond ffmpeg. One Python file, run by the Python that comes
  with macOS.

## What you need

- A Mac (Apple silicon or Intel) that's awake or can wake at the launch time.
- [Homebrew](https://brew.sh) and ffmpeg: `brew install ffmpeg`
- Apple's command line tools, for `/usr/bin/python3`: `xcode-select --install`
- Recordings arriving in a folder. For a Fetch TV box, see *Getting recordings off a
  Fetch box* below.

## Set up

```bash
git clone https://github.com/zot30/offair.git ~/offair && cd ~/offair

# 1. Your settings
mkdir -p ~/.config/offair
cp config.example.ini ~/.config/offair/config.ini
open -e ~/.config/offair/config.ini        # set the three folders, and the share if you use one

# 2. Test the settings. Fix anything marked FAIL and run it again.
./offair.py check

# 3. See what it would do. This changes nothing.
./offair.py --dry-run

# 4. Convert one recording now and look at the result in your media server.
./offair.py --force --limit 1

# 5. Install the nightly job, the status page and the Finder Quick Action.
./offair.py install
```

Then open <http://localhost:8765> to see the status page.

If your folders are on a network share, also do the one-time permission step under
*Network shares and macOS privacy*. Without it, the nightly job can't read the share.

To wake a sleeping Mac five minutes before the run:
`sudo pmset repeat wakeorpoweron MTWRFSU 04:25:00`

## Settings

All settings live in `~/.config/offair/config.ini`. `config.example.ini` lists every
one with an explanation. Only `[folders]` is required.

| Section | What it controls |
| --- | --- |
| `[folders]` | Where recordings arrive, and the TV and film library folders. |
| `[share]` | The network share those folders are on, if any. |
| `[schedule]` | The run window and the launch time. |
| `[encode]` | Deinterlacing, quality, audio, hardware encoder. |
| `[ads]` | Ad-break chapters (off by default). |
| `[plex]` | Plex's Skip Commercials button for new recordings (off by default). |
| `[files]` | Keeping originals, filing, which recordings qualify. |
| `[notifications]` | Notification title, share-down alert. |

Run `./offair.py check` after every change. It reports misspelt settings and bad values.
After changing `launch_at`, run `./offair.py install` again.

To use a different settings file, set `OFFAIR_CONFIG=/path/to/config.ini`.

## What a run does

1. It exits quietly in any of these cases, logging the reason:
   - another run is in progress
   - it's outside the run window
   - the Mac is on battery
   - the share isn't reachable and can't be mounted
2. It picks recordings that:
   - haven't been converted, and have failed fewer than 3 times
   - are over 50 MB
   - haven't been modified for 20 minutes and aren't growing

   Oldest recordings go first. Any folder or file whose name differs from a neighbour's
   only in case is skipped with a notification, because macOS can confuse them over SMB.
3. It converts them one at a time, keeping the Mac awake. The defaults are yadif
   deinterlacing, libx264 CRF 20 preset `fast`, AAC 192k and faststart. It keeps one
   video stream and one audio stream and drops teletext and data streams. Side-channel
   5.1 audio is converted to standard 5.1, which players need.
4. Each recording is copied to the Mac first, so ffmpeg only ever reads local files.
5. It checks the MP4, copies it back as `name.mp4.partial`, checks the size and renames
   it into place.
6. It files the MP4 into your libraries.
7. With `[plex] detect_ads = yes`, it asks Plex to find the ad breaks in what it filed.
8. You get a notification only when something needs you: a recording that has failed 3
   times, the share unreachable 3 nights running, a full disk, a copy that has hung for
   10 minutes (the run is stopped and retried next time), or an unexpected error.

On a MacBook Air M4, a 45-minute HD episode takes about 22 minutes, and SD runs about
ten times faster than real time.

## Filing

Recordings are expected in the layout fetchtv-cli saves:
`<Show_Name>/S16_E10_-_Episode_title_-_Wed_07_Jan.mpeg`.

- **Episodes** (names starting `S<n>_E<n>`) go to
  `<tv>/<Show Name>/Season 16/<Show Name> - S16E10 - Episode title.mp4`. The show name
  comes from the folder, with underscores turned into spaces. An existing show folder
  with the same name in different case is reused.
- **Films** (everything else) go to `<movies>/<Title>/<Title>.mp4`.
- **Nothing is overwritten.** If the show folder already has that SxxEyy anywhere in
  it, or a film folder with that title exists, the MP4 stays beside the recording and
  the log names what it found.
- Originals stay where they are until `keep_originals = no`.

## Status page

<http://localhost:8765> shows, on this Mac only:

- whether a run is in progress and which recording it's on, with a **Stop this run** button
- whether the recordings folder is reachable
- **Needs attention:** recordings that were given up on (with **Try again**), and MP4s
  that weren't filed because the library already had that episode or film
- **Waiting:** recordings not yet converted and why, each with **Convert now**
- the latest 40 conversions, where they were filed, and their ad breaks
- the end of the log

The page refreshes every minute. It never converts anything itself: its buttons leave
a request for the nightly job, exactly like the Quick Action below. Stopping a run
ends it cleanly, and the recording is tried again next time.

Only this Mac can open the page, and it refuses requests sent from other websites.
Change the port with `[status] port`, then run `./offair.py install` again.

## Convert now

To watch something sooner, use the status page, or right-click its recording in Finder and choose
**Quick Actions → Convert now**. It goes ahead of everything else, runs outside the
window and on battery, and you get a notification when it's filed or has failed. It
also retries a recording that was given up on.

If **Convert now** isn't in the menu, turn it on under Quick Actions → Customise…

## Ad breaks

There are two ways to mark ad breaks. Neither cuts anything out.

### Plex's Skip Commercials button

Plex (with Plex Pass) can find ad breaks itself and show a **Skip Commercials** button.
For libraries that aren't Plex DVR recordings, it only does so with the library's
**Ad detection** set to *For all items*, and then it scans the whole library during
maintenance, which takes a small NAS days.

With `[plex] detect_ads = yes`, offair gets the button onto new recordings only. For
each MP4 it has filed, it waits for Plex to add it, switches that library's Ad
detection to *For all items* for the moment it takes to request a scan of that one
item, and switches it straight back. It does one recording at a time, because Plex
drops requests that are queued behind a running scan. The scan runs on the Plex
server. On a DS418j a scan took between a fifth of the running time and one and a half
times it, depending on what else the NAS was doing.

Plex doesn't say when a scan has finished, so offair waits until ad breaks appear, or
for 2.5 times the running time before recording "none found". A recording with no ads
therefore holds up the ones after it; any not reached by the end of the run window
are done on the next run.

To set it up:

1. In Plex, set Settings → Library → **Generate ad video markers** to *as a scheduled
   task*, and leave each library's **Ad detection** on *For recorded items*.
2. Save your [Plex token](https://support.plex.tv/articles/204059436) in a file of its
   own: `pbpaste > ~/.config/offair/plex-token && chmod 600 ~/.config/offair/plex-token`
3. In `config.ini`, set `[plex] detect_ads = yes` and `url` (for example
   `http://nas.local:32400`), then run `./offair.py check`.

The status page shows the result for each recording. If Plex is unreachable, or hasn't
added the file within ten minutes, the recording is tried again on the next run.
Nothing here can fail a conversion. offair uses Plex's own interface for this and
writes nothing into Plex's database, but the interface is undocumented and could
change with a Plex update.

### Chapters with Comskip

With `[ads] detect = yes`, [Comskip](https://github.com/erikkaashoek/Comskip) scans
each recording and every ad break becomes a chapter called `Ads` in the MP4, with
`Part 1`, `Part 2`… between them. Nothing is cut out, so a wrong guess costs nothing.
Comskip takes about 1.5 minutes for a 30-minute HD recording. If it's missing, fails
or finds nothing, the recording is converted without chapters.

Comskip isn't in Homebrew. To build it into `~/.local/bin` without sudo:

```bash
brew install autoconf automake libtool argtable
git clone --depth 1 https://github.com/erikkaashoek/Comskip.git ~/Comskip && cd ~/Comskip
git apply ~/offair/extras/comskip-ffmpeg8.patch      # only needed with ffmpeg 8 or later
./autogen.sh && ./configure --prefix="$HOME/.local" && make && make install
```

Comskip links against Homebrew's ffmpeg libraries. If it stops working after a major
ffmpeg upgrade, run `make clean && make && make install` again.

## Network shares and macOS privacy

When launchd runs the job, macOS blocks it from reading network volumes until you
allow it. Running from Terminal works without this, which hides the problem until the
first night.

1. Open System Settings → Privacy & Security → Full Disk Access.
2. Click **+**, press ⌘⇧G and enter `/usr/bin/python3`.
3. Make sure it's switched on.

This covers anything launchd runs with `/usr/bin/python3`, not only offair. ffmpeg
needs no permission, because it only reads local copies. If macOS asks to let ffmpeg
or ffprobe access a network volume, you can deny it.

Then test it the way the nightly job runs:

```bash
launchctl kickstart gui/$(id -u)/offair.convert
tail ~/Library/Logs/offair.log ~/Library/Logs/offair.launchd.log
```

Outside the run window this logs "Outside run window" and exits, which is enough to
show the job starts. A "macOS privacy settings block reading" line means the
permission above is missing.

## Where things are

| | |
| --- | --- |
| Settings | `~/.config/offair/config.ini` |
| Log | `~/Library/Logs/offair.log` (rotated at 1 MB) |
| What's been converted, and failures | `~/Library/Application Support/offair/state.json` |
| Working files | `~/Library/Caches/offair/` |
| Nightly job and status page | `~/Library/LaunchAgents/offair.convert.plist`, `offair.status.plist` |

To retry a recording that hit the failure limit, use **Convert now**, or delete its
entry from `state.json`.

## Getting recordings off a Fetch box

`extras/fetch-sync.sh` is an example nightly download script built on fetchtv-cli.
Run it on a machine that's always on, an hour or so before offair's launch time. Its
header explains the setup. It chooses what to download; offair converts whatever arrives.

Once `keep_originals = no`, offair deletes each recording after converting it.
fetchtv-cli won't download it again as long as its `fetchtv_save_list.json` is left
alone.

## Tests

```bash
/usr/bin/python3 -m unittest discover tests -v
```

The tests run in temporary folders and include real conversions of generated
recordings. They don't touch your settings, recordings or libraries.

## Uninstall

```bash
./offair.py uninstall
```

This removes the nightly job and the Quick Action and lists the files it leaves
(settings, state, log, working folder) for you to delete.

## Licence

MIT. See `LICENSE`.
