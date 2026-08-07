#!/usr/bin/env python3
"""
Sydney Matinee Finder v3.0

Scrapes upcoming classical and community orchestra performances in Sydney,
with a focus on matinees (before 5:00 PM), and writes:

    data/events.json    the site's only data input, rendered by index.html
    data/cache.json     extracted-result HTTP cache, committed for warm runs
    feeds/*.ics         calendar subscription feeds

Usage:
    python fetch_events.py                 scrape and write everything
    python fetch_events.py --no-fetch      rebuild outputs from cache only
    python fetch_events.py --summary       print a per-source status table
    python fetch_events.py --check-freshness   exit 1 if a source is stale

Exit codes matter: a non-zero exit fails the GitHub Actions run, which is
how a collapsed scrape becomes an email rather than a silently blank site.

Data sources: Sydney Symphony Orchestra, Sydney Opera House, Willoughby
Symphony Orchestra, The Concourse (Chatswood), North Sydney Symphony
Orchestra. All read-only, identified with a contact URL in the User-Agent.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List
import json
import sys

import core
from core import Performance
import sources
from sources import Fetcher, SourceError

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
FEEDS_DIR = ROOT / "feeds"
EVENTS_PATH = DATA_DIR / "events.json"
CACHE_PATH = DATA_DIR / "cache.json"


# =============================================================================
# PERSISTENCE
# =============================================================================

def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


def save_cache(cache: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"), ensure_ascii=False)
        f.write("\n")


def item_to_performance(item: dict) -> Performance:
    """Rebuild a Performance from a previously published item (carry-over)."""
    time_part = item.get("time") or "00:00"
    dt = datetime.fromisoformat(f"{item['date']}T{time_part}:00")
    return Performance(
        performer=item.get("performer", ""),
        title=item.get("raw_title") or item.get("title", ""),
        date=dt,
        venue_name=item.get("venue_name", ""),
        venue_address=item.get("venue_address", ""),
        url=item.get("url", ""),
        source=item.get("source", ""),
        time_confirmed=bool(item.get("time_confirmed")),
        stale=True,
    )


# =============================================================================
# PER-SOURCE DEGRADATION
# =============================================================================

def previous_source_state(previous: dict) -> Dict[str, dict]:
    return {s["name"]: s for s in previous.get("sources", [])}


def decide_source(name: str, scraped: List[Performance], error: str, kind: str,
                  prev_state: dict, previous_items: List[dict],
                  config: dict) -> tuple:
    """
    Decide what this source contributes, and its reported status.

    Health is judged on the PRE-dedup scrape count. Willoughby scrapes ~8
    performances but contributes 1 after The Concourse supplies exact times
    for the rest - judging on the final contribution would read a working
    merge as an outage and never detect a real one.

    Returns (performances, status_dict).
    """
    floors = config.get("source_floors", {})
    floor = floors.get(name, 1)
    ratio = config.get("source_degrade_ratio", 0.6)
    stale_max = config.get("stale_max_days", 14)

    prev = prev_state.get(name, {})
    prev_count = prev.get("count", 0)
    last_success = prev.get("last_success")
    now_iso = core.now_sydney().isoformat(timespec="seconds")

    healthy = (not error) and len(scraped) >= floor
    collapsed = (not error) and prev_count >= floor and len(scraped) < ratio * prev_count

    if healthy and not collapsed:
        return scraped, {
            "name": name, "status": "ok", "count": len(scraped),
            "previous_count": prev_count, "last_success": now_iso, "message": "",
        }

    # Something is wrong. Can we still stand behind the previous data?
    days_stale = None
    if last_success:
        try:
            days_stale = (core.now_sydney() - datetime.fromisoformat(last_success)).days
        except ValueError:
            days_stale = None

    reason = error or (f"count collapsed {prev_count} -> {len(scraped)}"
                       if collapsed else f"only {len(scraped)} events (floor {floor})")

    if days_stale is not None and days_stale > stale_max:
        # Serving month-old listings as if current is worse than showing nothing.
        return [], {
            "name": name, "status": "failed", "count": 0,
            "previous_count": prev_count, "last_success": last_success,
            "message": f"{reason}; last good data {days_stale} days ago, dropped",
        }

    carried = [item_to_performance(i) for i in previous_items
               if i.get("source") == name]
    if not carried:
        return [], {
            "name": name, "status": "failed", "count": 0,
            "previous_count": prev_count, "last_success": last_success,
            "message": reason,
        }

    # Discard the partial fresh result rather than merging it: merging
    # resurrects cancelled events and creates near-duplicates.
    return carried, {
        "name": name, "status": "stale", "count": len(carried),
        "previous_count": prev_count, "last_success": last_success,
        "message": f"{reason}; showing last good data ({kind or 'error'})",
    }


# =============================================================================
# CLI HELPERS
# =============================================================================

def cmd_summary() -> int:
    """Markdown table for the GitHub Actions job summary."""
    data = load_json(EVENTS_PATH, None)
    if not data:
        print("No data/events.json found - the fetch may have failed.")
        return 0

    counts = data.get("counts", {})
    print(f"### Matinee Finder — {data.get('generated_at', 'unknown')}\n")
    print(f"**{counts.get('total', 0)} events** · "
          f"{counts.get('matinee', 0)} matinees · "
          f"{counts.get('public', 0)} publicly bookable · "
          f"{counts.get('needs_review', 0)} need review\n")
    print("| Source | Status | Events | Previous | Note |")
    print("|---|---|---:|---:|---|")
    icon = {"ok": "✅", "stale": "⚠️", "failed": "❌"}
    for s in data.get("sources", []):
        print(f"| {s['name']} | {icon.get(s['status'], '')} {s['status']} | "
              f"{s['count']} | {s.get('previous_count', 0)} | {s.get('message', '')} |")
    return 0


def cmd_check_freshness() -> int:
    """
    Exit 1 if any source has been failing for too long. Runs after the
    commit, so the data still publishes - this only turns the run red so
    the failure reaches your inbox.
    """
    config = core.load_config(CONFIG_PATH)
    data = load_json(EVENTS_PATH, None)
    if not data:
        print("No events.json to check.")
        return 1

    limit = config.get("freshness_warn_days", 3)
    problems = []
    for s in data.get("sources", []):
        if s.get("status") == "ok":
            continue
        last = s.get("last_success")
        if not last:
            problems.append(f"{s['name']}: never succeeded ({s.get('message', '')})")
            continue
        try:
            days = (core.now_sydney() - datetime.fromisoformat(last)).days
        except ValueError:
            continue
        if days > limit:
            problems.append(f"{s['name']}: no fresh data for {days} days "
                            f"({s.get('message', '')})")

    if problems:
        print("Stale sources:")
        for p in problems:
            print("  -", p)
        return 1
    print("All sources fresh.")
    return 0


# =============================================================================
# MAIN
# =============================================================================

def main(argv: List[str]) -> int:
    if "--summary" in argv:
        return cmd_summary()
    if "--check-freshness" in argv:
        return cmd_check_freshness()

    offline = "--no-fetch" in argv

    print("=" * 62)
    print("Sydney Matinee Finder v3.0")
    print("=" * 62)
    if core.SYDNEY_TZ is None:
        print("[NOTE] zoneinfo/tzdata unavailable - using built-in DST rule.")
        print("       For guaranteed accuracy run: pip install tzdata")
    if offline:
        print("[MODE] --no-fetch: rebuilding from cache only, no network.")
    print()

    config = core.load_config(CONFIG_PATH)
    previous = load_json(EVENTS_PATH, {})
    previous_items = previous.get("items", [])
    prev_state = previous_source_state(previous)
    cache = load_json(CACHE_PATH, {})

    fetcher = Fetcher(config, cache, offline=offline)

    all_performances: List[Performance] = []
    source_status: List[dict] = []

    for name, scraper in sources.SCRAPERS:
        print(f"Scraping {name}...")
        scraped, error, kind = [], "", ""
        try:
            scraped = scraper(fetcher)
            print(f"  scraped {len(scraped)} performances")
        except SourceError as e:
            error, kind = str(e), e.kind
            print(f"  [{e.kind.upper()}] {e}")
        except Exception as e:                     # noqa: BLE001 - never let
            error, kind = f"{type(e).__name__}: {e}", "broken"   # one source
            print(f"  [BROKEN] {error}")           # kill the whole run

        contributed, status = decide_source(
            name, scraped, error, kind, prev_state, previous_items, config)
        if status["status"] != "ok":
            print(f"  -> {status['status']}: {status['message']}")
        all_performances.extend(contributed)
        source_status.append(status)

    print()
    print(f"Total collected: {len(all_performances)}")

    # Order matters: carry-over already happened, so dedupe now sees stale
    # and fresh copies of the same concert and collapses them.
    unique = core.deduplicate_performances(all_performances)
    unique = core.suppress_unconfirmed_duplicates(unique)
    print(f"After deduplication: {len(unique)}")

    # Ordinals first: classify() resolves manual overrides by event id, and
    # the id depends on the ordinal.
    core.assign_ordinals(unique)
    for perf in unique:
        core.classify(perf, config)

    today_start = core.today_start_sydney()
    future = [p for p in unique if p.date >= today_start]
    future = core.sort_performances(future)
    print(f"Future events: {len(future)}")

    payload = core.build_payload(future, source_status, config)
    counts = payload["counts"]
    print(f"  matinees: {counts['matinee']} "
          f"({counts['public_matinee']} publicly bookable)")
    print(f"  time TBA: {counts['tba']}   needs review: {counts['needs_review']}")
    print(f"  carried over as stale: {counts['stale']}")

    if not core.sanity_check(payload["items"], EVENTS_PATH, config):
        print("\nNothing written. Existing data left untouched.")
        return 1

    if all(s["status"] != "ok" for s in source_status):
        print("\nSANITY FAIL: no source returned fresh data. Nothing written.")
        return 1

    DATA_DIR.mkdir(exist_ok=True)
    FEEDS_DIR.mkdir(exist_ok=True)

    core.write_events_json(payload, EVENTS_PATH)
    print(f"\nWrote {EVENTS_PATH.relative_to(ROOT)}")

    eligible = [i for i in payload["items"] if core.eligible_for_feeds(i)]
    for feed in core.feed_definitions(config):
        items = [i for i in eligible if feed["filter"](i)]
        (FEEDS_DIR / feed["file"]).write_text(
            core.build_ics(items, feed["name"], config), encoding="utf-8", newline="")
        print(f"Wrote feeds/{feed['file']} ({len(items)} events)")

    evicted = fetcher.evict_stale()
    save_cache(cache)
    print(f"Cache: {fetcher.stats['fetched']} fetched, "
          f"{fetcher.stats['from_cache']} from cache, "
          f"{fetcher.stats['failed']} failed, {evicted} evicted")

    print()
    print("=" * 62)
    print("Done. Run refresh.bat to view locally, or push to publish.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    # Propagate the exit code: the sanity check must be able to fail the
    # GitHub Actions run rather than exiting 0 with bad data.
    sys.exit(main(sys.argv[1:]))
