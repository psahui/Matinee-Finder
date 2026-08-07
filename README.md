# Sydney Matinee Finder

Finds upcoming classical and orchestral concerts around Sydney and highlights the
**matinees** — performances starting before 5:00 PM. It refreshes itself daily and
publishes to a static site, with calendar feeds you can subscribe to.

**Live site:** https://psahui.github.io/Matinee-Finder/

The problem it solves: matinee performances are scattered across a dozen venue and
orchestra websites, each with its own format, and none of them let you filter by
"afternoon". This gathers them into one chronological list.

## How it works

```
config.json ──┐
              ├─> fetch_events.py ──> data/events.json ──> index.html (renders + filters)
5 websites ───┘        │                                        client-side, no build step
                       ├──> data/cache.json   (extracted-result HTTP cache)
                       └──> feeds/*.ics       (calendar subscriptions)
```

A GitHub Actions cron job runs the scraper each morning and commits the results back
to `main`; GitHub Pages serves the repository root. There is no server and no build
step — `index.html` is plain HTML, CSS and JavaScript with no dependencies.

### Sources

| Source | Method |
|---|---|
| [Sydney Symphony Orchestra](https://www.sydneysymphony.com/) | JSON API (UTC timestamps) |
| [Sydney Opera House](https://www.sydneyoperahouse.com/whats-on) | Listing pagination + per-event "Dates and times" tables, JSON-LD fallback |
| [Willoughby Symphony Orchestra](https://www.willoughbysymphony.com.au/Events) | Event pages |
| [The Concourse, Chatswood](https://www.theconcourse.com.au/genre/music-classical/) | Genre listing + event pages |
| [North Sydney Symphony Orchestra](https://www.nsso.org.au/concerts) | Season content files behind the site's JS shell |

Sydney Symphony events presented at the Opera House are taken from the SSO API rather
than the Opera House listing, because the API carries authoritative times. The
Concourse doubles as a crosscheck for Willoughby Symphony, whose own site often omits
performance times that The Concourse publishes exactly.

## Design decisions worth knowing

**Times are never guessed.** An earlier version defaulted missing times to 7:30 PM,
which silently invented data. Now an event with no published time is shown as
"Time TBA", is never badged as a matinee, and never enters a calendar feed. Where two
sources describe the same concert, the one with a confirmed time wins.

**Daylight saving is handled exactly.** Sydney switches on the first Sunday of October
and back on the first Sunday of April. A month-based approximation is wrong for about
four weeks a year — it showed late-October concerts an hour early and mislabelled 5 PM
performances as matinees. Conversion uses the IANA database via `zoneinfo`, with the
real first-Sunday rule as a fallback. Nothing in the codebase may use a naive
`datetime.now()`: CI runners are UTC, where a 6:45 PM run is already the next day in
Sydney.

**Two independent classification axes.** *Access* is who may book — `public`,
`schools` (the Sydney Symphony's "For Schools" performances are quoted per student and
booked by teachers, not sold to the public), or `participants` (workshops you attend to
play in, not listen to; the Young Musicians Workshop page states outright that it "is
not a performance"). *Format* is what kind of event it is — concert, film in concert,
family, open rehearsal, masterclass, short format. They are deliberately separate: a
10 AM schools concert genuinely *is* a matinee, it simply isn't bookable. Open
rehearsals and masterclasses **are** publicly bookable and stay on by default.

Classification is keyword-driven from `config.json`, so it is guesswork. Anything whose
title hints at a restriction that no rule caught is flagged `needs_review` and shown
with a "check category" badge, because the expensive mistake is sending someone to a
concert they cannot get into.

**It degrades honestly.** If a source breaks, the last known-good listings for that
source are carried forward, marked "last known", and the site header shows the source
in amber or red. After 14 days without a successful fetch the source is dropped rather
than serving stale listings indefinitely. If the total event count collapses below 70%
of the previous run, nothing is written at all and the workflow fails loudly — a red
build and an email beat a silently blank page.

**The cache stores extractions, not pages.** Caching raw HTML for ~80 event pages would
add megabytes to every daily commit. `data/cache.json` holds only the parsed fields per
URL, with TTLs tiered by how soon each performance is: imminent concerts are rechecked
every two days, distant ones every fortnight. The request delay applies only to real
network calls, so a warm run makes a handful of requests instead of eighty.

## Running it locally

```bash
pip install -r requirements.txt
```

Then double-click `refresh.bat` (Windows), or:

```bash
python fetch_events.py
python -m http.server 8000     # then open http://localhost:8000/
```

The page fetches its data with `fetch()`, which browsers block on `file://` URLs, so it
must be served over HTTP — opening `index.html` directly shows an error.

| Command | Effect |
|---|---|
| `python fetch_events.py` | Scrape and rebuild everything |
| `python fetch_events.py --no-fetch` | Rebuild outputs from cache, no network |
| `python fetch_events.py --summary` | Per-source status table |
| `python fetch_events.py --check-freshness` | Exit 1 if a source has been failing |

## Calendar feeds

| Feed | Contents |
|---|---|
| `feeds/matinees.ics` | Everything before 5 PM |
| `feeds/weekend-matinees.ics` | Saturday and Sunday afternoons only |
| `feeds/all.ics` | All concerts |

Only publicly bookable events with a confirmed time are included. Times are emitted in
UTC rather than as a timezone reference, so every client renders them correctly.

## Scraping conduct

The scraper identifies itself honestly (`SydneyMatineeFinder/3.0` with a contact URL),
waits a couple of seconds between requests with jitter, caches aggressively to minimise
repeat traffic, and reads only public listing pages.

The Sydney Opera House sits behind bot protection. **This project does not spoof a
browser User-Agent to get around it.** If that source is blocked, the site degrades
visibly and the fix is to ask the Opera House, not to disguise the request. If you
maintain one of these sites and would like the scraper to stop or change behaviour,
open an issue and it will be honoured.

Only factual details are republished — date, time, title, venue — and every listing
links back to the venue's own page for booking. No descriptions, images or pricing are
copied.

## Repository layout

```
index.html            frontend: renders and filters events.json
fetch_events.py       entry point, orchestration, per-source degradation
core.py               data model, timezones, parsing, classification, .ics output
sources.py            cached HTTP layer and the five scrapers
config.json           classification rules, aliases, cache TTLs, source floors
data/                 generated: events.json and the HTTP cache (committed)
feeds/                generated: .ics subscription feeds (committed)
archive/              the 2025 Jupyter notebook prototype, kept for reference
```

## Licence

Code is MIT licensed — see `LICENCE`.

The concert data in `data/` and `feeds/` is **not** covered by that licence. It is
factual information belonging to the venues and ensembles listed above, gathered from
their public pages and reproduced here with attribution and links back to the source.
