#!/bin/bash
# fetch-sync.sh: example download step for a Fetch TV box, using fetchtv-cli
# (https://pypi.org/project/fetchtv-cli). It copies new recordings from the box into the
# folder offair watches. Run it nightly, before offair's launch time, on any machine
# that is always on (a NAS scheduled task, for example).
#
# Which recordings it downloads: every film, every recording with an id of at least
# NEW_FROM_ID, and every recording of the shows named in PULL_SHOWS.
#
# Setup:
#   python3 -m venv ~/fetchtv-venv && ~/fetchtv-venv/bin/pip install fetchtv-cli
#   ~/fetchtv-venv/bin/fetchtv --info        # finds the box and prints its address
# Then edit the four settings below. Try it with:  DRY_RUN=1 ./fetch-sync.sh

FETCH_IP=192.168.1.50                  # the Fetch box's address
FETCH_PORT=49152
VENV="$HOME/fetchtv-venv"              # where fetchtv-cli is installed
DEST=/path/to/Recordings               # offair's [folders] recordings, as this machine sees it

DRY_RUN=${DRY_RUN:-0}                  # 1 = only list what would be downloaded
MIN_MOVIE_MINUTES=80                   # non-episode recordings at least this long count as films
PULL_SHOWS="${PULL_SHOWS:-}"           # shows to download in full, one per line
SKIP_SHOWS=""                          # shows never downloaded, one per line
NEW_FROM_ID=0                          # download every recording with an id at least this (ids grow
                                       # over time; 0 = everything on the box)

FT="$VENV/bin/fetchtv --ip $FETCH_IP --port $FETCH_PORT"
PY="$VENV/bin/python"
LOG="$DEST/fetchtv.log"
JSON=/tmp/fetch-recordings.json
WANT=/tmp/fetch-wanted.tsv
LOCK=/tmp/fetch-sync.lock

mkdir -p "$DEST"
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date) ==="

mkdir "$LOCK" 2>/dev/null || { echo "Already running, exiting"; exit 0; }
trap 'rmdir "$LOCK"' EXIT

OUT=$($FT --isrecording 2>&1)
if ! echo "$OUT" | grep -q "No recordings found"; then
  echo "Fetch is recording or not reachable, skipping this run"
  exit 0
fi

$FT --recordings --json > "$JSON" || { echo "Could not list recordings"; exit 1; }

JSON="$JSON" PULL_SHOWS="$PULL_SHOWS" SKIP_SHOWS="$SKIP_SHOWS" NEW_FROM_ID="$NEW_FROM_ID" MIN_MOVIE_MINUTES="$MIN_MOVIE_MINUTES" \
$PY - > "$WANT" <<'PYEOF'
import json, os
text = open(os.environ["JSON"]).read()
data, _ = json.JSONDecoder().raw_decode(text[text.find("[\n"):])
pull = {s.strip().lower() for s in os.environ["PULL_SHOWS"].splitlines() if s.strip()}
skip = {s.strip().lower() for s in os.environ["SKIP_SHOWS"].splitlines() if s.strip()}
min_secs = int(os.environ["MIN_MOVIE_MINUTES"]) * 60
new_from = int(os.environ["NEW_FROM_ID"])
for show in data:
    name = show["title"].lower()
    if name in skip:
        continue
    for item in show.get("items", []):
        # The box sometimes reports no duration (0) for new recordings; count those as films too
        dur = item.get("duration", 0)
        movie = item.get("type") != "episode" and (dur >= min_secs or dur <= 0)
        new = int(item["id"]) >= new_from
        if movie or new or name in pull:
            print(show["title"] + "\t" + item["title"])
PYEOF

if [ ! -s "$WANT" ]; then echo "Nothing selected"; exit 0; fi
echo "Selected:"; cat "$WANT"
if [ "$DRY_RUN" = 1 ]; then echo "Dry run, not downloading"; exit 0; fi

# fetchtv-cli remembers what it has saved in $DEST/fetchtv_save_list.json and never
# downloads a recording twice, even after offair deletes the original. Leave that file alone.
while IFS=$'\t' read -r folder title; do
  echo "--- $folder / $title"
  $FT --recordings --folder "$folder" --title "$title" --save "$DEST" < /dev/null
done < "$WANT"
echo "Finished"
