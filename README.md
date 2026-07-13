# Sydney Matinee Finder

Finds upcoming classical and community orchestra performances in Sydney, with a
focus on **matinees** (shows starting before 5:00 PM). Produces a single
self-contained HTML page with matinees highlighted in yellow.

The active version lives in the **`Claude/`** folder.
(`MatineeFinder.ipynb` in the repo root is the original 2025 Jupyter-notebook
prototype, kept for reference.)

## Data sources

| Source | Method |
|---|---|
| Sydney Symphony Orchestra | JSON API (UTC timestamps converted to Sydney time) |
| Sydney Opera House | What's On listing + per-event "Dates and times" tables |
| Willoughby Symphony Orchestra | Events pages |
| The Concourse, Chatswood | Classical Music genre listing (also crosschecks Willoughby times, incl. Sunday 2 PM matinees) |
| North Sydney Symphony Orchestra | Season content files |

Duplicate listings of the same performance across sites are merged, with
exact-time entries preferred over "Time TBA" ones. Times are never guessed:
an event whose start time can't be found is shown as "Time TBA" and is never
marked as a matinee.

## Usage (Windows)

One-off setup:

```
pip install -r Claude/requirements.txt
```

Then either **double-click `Claude/refresh.bat`**, or run:

```
cd Claude
python sydney_matinee_finder.py
```

The run takes ~4 minutes (the script waits 2 seconds between page fetches to
be polite to the websites) and writes `Claude/sydney_matinees.html`, which
opens in any browser.

## Reading the output

- Yellow rows with a **MATINEE** badge start before 5:00 PM.
- Grey **TIME TBA** badge: the source only published a date.
- The header shows an event count per source. A count of **0 with a ⚠**
  means that website probably changed its structure and that scraper needs
  updating.

## Notes

- Timezone handling uses the IANA database (`zoneinfo` + `tzdata`), so
  daylight-saving transitions (first Sunday of October / April) are exact.
- The script is read-only towards the target websites and identifies itself
  with a custom User-Agent.
