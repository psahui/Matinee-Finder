#!/usr/bin/env python3
"""
Network layer and the five scrapers.

Caching note: this caches the small STRUCTURED EXTRACTION from each page,
not the raw HTML. Caching ~80 pages of raw markup would add megabytes to
every daily commit and make the repo unclonable within a year; caching the
handful of fields we actually parsed keeps data/cache.json to tens of KB
and makes the daily diff readable.

Politeness note: the request delay lives inside the network path, so cache
hits cost nothing. A warm run makes roughly six requests instead of eighty.

Bot-protection note: the Sydney Opera House sits behind Kasada. We identify
honestly with a contact URL and accept being blocked if it comes to that -
see the per-source carry-over in fetch_events.py. We deliberately do NOT
spoof a browser User-Agent to get around it.
"""

from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple
import json
import random
import re
import time

import requests
from bs4 import BeautifulSoup

from core import (
    DATE_ONLY_PATTERN, DAY_NAMES, MONTH_NAMES, Performance,
    extract_explicit_datetimes, lookup_venue_address, now_sydney,
    parse_date_flexible, parse_iso_datetime,
)

CACHE_SCHEMA = 1
USER_AGENT_TEMPLATE = "SydneyMatineeFinder/3.0 (+{contact})"


class SourceError(Exception):
    """Raised when a source cannot be scraped. Carries a reason category."""

    def __init__(self, message: str, kind: str = "broken"):
        super().__init__(message)
        self.kind = kind          # "blocked" | "broken"


# =============================================================================
# CACHE
# =============================================================================

# Hosts whose query strings are pure cache-busters and must be ignored when
# keying the cache. Everywhere else the query is meaningful - stripping it
# globally collapses ?page=0, ?page=1... onto one key and silently serves
# the first page's results for every page.
CACHE_BUSTER_HOSTS = ("storage.googleapis.com",)


def canonical_url(url: str) -> str:
    """Cache key for a URL."""
    if any(host in url for host in CACHE_BUSTER_HOSTS):
        return url.split("?")[0]
    return url


class Fetcher:
    """Cached, polite, retrying HTTP with per-source failure reporting."""

    def __init__(self, config: dict, cache: dict, offline: bool = False):
        self.config = config
        self.cache = cache
        self.offline = offline
        self.session = requests.Session()
        self.stats = {"fetched": 0, "from_cache": 0, "failed": 0}
        self.seen_keys = set()

        contact = config.get("contact_url", "")
        self.headers = {
            "User-Agent": USER_AGENT_TEMPLATE.format(contact=contact),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-AU,en;q=0.9",
        }
        self.json_headers = dict(self.headers, Accept="application/json")

    # -- cache bookkeeping ---------------------------------------------------

    def _ttl_days(self, tier: str) -> float:
        return self.config.get("cache_ttl_days", {}).get(tier, 1)

    def tier_for_event(self, event_date: Optional[datetime]) -> str:
        """
        Imminent performances change (times shift, shows cancel); ones a year
        out do not. Tier the TTL off how soon the event actually is.
        """
        if event_date is None:
            return "event_mid"
        days = (event_date - now_sydney()).days
        if days <= self.config.get("cache_near_days", 7):
            return "event_near"
        if days <= self.config.get("cache_mid_days", 60):
            return "event_mid"
        return "event_far"

    def cached(self, url: str, tier: str) -> Optional[dict]:
        """Return a still-fresh cached extraction, or None."""
        key = canonical_url(url)
        self.seen_keys.add(key)
        entry = self.cache.get(key)
        if not entry or entry.get("v") != CACHE_SCHEMA:
            return None

        ttl = self._ttl_days("failed" if not entry.get("ok") else tier)
        try:
            fetched = datetime.fromisoformat(entry["fetched_at"])
        except (KeyError, ValueError):
            return None
        if (now_sydney() - fetched) > timedelta(days=ttl):
            return None

        self.stats["from_cache"] += 1
        return entry.get("result")

    def store(self, url: str, result: dict, ok: bool = True, status: int = 200) -> None:
        key = canonical_url(url)
        self.seen_keys.add(key)
        self.cache[key] = {
            "v": CACHE_SCHEMA,
            "fetched_at": now_sydney().isoformat(timespec="seconds"),
            "ok": ok,
            "http_status": status,
            "result": result,
        }

    def evict_stale(self) -> int:
        """Drop entries no listing has referenced for a while."""
        cutoff = now_sydney() - timedelta(days=self.config.get("cache_evict_after_days", 30))
        drop = []
        for key, entry in self.cache.items():
            if key in self.seen_keys:
                continue
            try:
                if datetime.fromisoformat(entry["fetched_at"]) < cutoff:
                    drop.append(key)
            except (KeyError, ValueError):
                drop.append(key)
        for key in drop:
            del self.cache[key]
        return len(drop)

    # -- network -------------------------------------------------------------

    def _sleep(self) -> None:
        """
        Delay only on real requests. Jitter keeps the cadence off a metronome,
        which is both politer and less bot-like to rate limiters.
        """
        base = self.config.get("request_delay_seconds", 2.0)
        jitter = self.config.get("request_jitter_seconds", 0.0)
        time.sleep(base + random.uniform(0, jitter))

    def get(self, url: str, as_json: bool = False):
        """
        Fetch with retries and backoff.

        Raises SourceError(kind="blocked") on 403/429 so the caller can say
        "the runner is being bot-blocked" rather than "the site changed its
        markup" - two very different problems with different fixes.
        """
        if self.offline:
            raise SourceError(f"offline mode: refusing to fetch {url}", "broken")

        headers = self.json_headers if as_json else self.headers
        attempts = self.config.get("max_retries", 3)
        timeout = self.config.get("request_timeout_seconds", 30)
        last = None

        for attempt in range(attempts):
            self._sleep()
            try:
                resp = self.session.get(url, headers=headers, timeout=timeout)
                if resp.status_code in (403, 429):
                    self.stats["failed"] += 1
                    raise SourceError(
                        f"HTTP {resp.status_code} (bot protection?) for {url}", "blocked")
                resp.raise_for_status()
                self.stats["fetched"] += 1
                return resp.json() if as_json else resp.text
            except SourceError:
                raise
            except requests.RequestException as e:
                last = e
                if attempt < attempts - 1:
                    time.sleep(4 * (attempt + 1))

        self.stats["failed"] += 1
        raise SourceError(f"{url}: {last}", "broken")


# =============================================================================
# SCRAPERS
# =============================================================================
# Each returns a list of Performance. Raising SourceError marks the whole
# source failed so the caller can carry forward the previous run's data.

def scrape_sydney_symphony(fetcher: Fetcher) -> List[Performance]:
    """
    Sydney Symphony Orchestra via their JSON API - structured, authoritative
    times, and the single largest source. Timestamps are UTC with a Z suffix.
    """
    api_url = "https://www.sydneysymphony.com/api/events"

    cached = fetcher.cached(api_url, "listing")
    if cached is not None:
        events = cached.get("events", [])
    else:
        # Extract before caching. The raw API response is ~600KB of
        # descriptions and image metadata; storing it whole would add that
        # to every daily commit for the sake of four fields per event.
        data = fetcher.get(api_url, as_json=True)
        events = []
        for event in data.get("docs", []):
            if not isinstance(event, dict):
                continue
            title = (event.get("title") or "").strip()
            if not title:
                continue

            venue_data = event.get("venue", {})
            venue_name = (venue_data.get("title") if isinstance(venue_data, dict) else None) \
                or "Concert Hall, Sydney Opera House"

            instances_data = event.get("eventInstances", {})
            if isinstance(instances_data, dict):
                instances = instances_data.get("docs", [])
            elif isinstance(instances_data, list):
                instances = instances_data
            else:
                instances = []
            if not instances and event.get("startDate"):
                instances = [{"startDate": event["startDate"]}]

            starts = [i["startDate"] for i in instances
                      if isinstance(i, dict) and i.get("startDate")]
            if starts:
                events.append({"title": title, "slug": event.get("slug", ""),
                               "venue": venue_name, "starts": starts})
        fetcher.store(api_url, {"events": events})

    performances: List[Performance] = []
    seen = set()
    for event in events:
        title = event["title"]
        slug = event.get("slug", "")
        url = (f"https://www.sydneysymphony.com/events/{slug}" if slug
               else "https://www.sydneysymphony.com/concert-tickets/whats-on")
        venue_name = event.get("venue") or "Concert Hall, Sydney Opera House"

        for start in event.get("starts", []):
            dt = parse_iso_datetime(start)
            if not dt:
                continue
            key = f"{title}|{dt:%Y-%m-%d %H:%M}"
            if key in seen:
                continue
            seen.add(key)
            performances.append(Performance(
                performer="Sydney Symphony Orchestra",
                title=title,
                date=dt,
                venue_name=venue_name,
                venue_address=lookup_venue_address(venue_name),
                url=url,
                source="Sydney Symphony Orchestra",
            ))

    return performances


def scrape_sydney_opera_house(fetcher: Fetcher) -> List[Performance]:
    """
    Sydney Opera House classical listing.

    The listing pages are server-rendered Drupal (div.card--event). Events
    presented by the SSO are skipped because the SSO API already supplies
    them with authoritative times. For the rest, each event page carries a
    "Dates and times" table; JSON-LD is the fallback, and its startDate is
    UTC with no timezone marker.
    """
    base = "https://www.sydneyoperahouse.com"
    max_pages = fetcher.config.get("max_event_pages", 200)
    cards: List[Tuple[str, str, str]] = []
    seen_hrefs = set()

    for page in range(0, 8):
        listing_url = f"{base}/whats-on?genre%5B%5D=1436&page={page}"
        cached = fetcher.cached(listing_url, "listing")
        if cached is not None:
            page_cards = cached.get("cards", [])
        else:
            html = fetcher.get(listing_url)
            soup = BeautifulSoup(html, "html.parser")
            page_cards = []
            for card in soup.select("div.card--event"):
                link = card.select_one("a.card__link")
                if not link:
                    continue
                href = link.get("href", "")
                title = link.get_text(strip=True)
                if not href or not title:
                    continue
                lines = [t.strip() for t in card.get_text("\n").split("\n") if t.strip()]
                venue = lines[-1] if lines else "Sydney Opera House"
                if "streamline" in venue.lower():   # icon alt-text noise
                    venue = "Sydney Opera House"
                page_cards.append({"href": href, "title": title, "venue": venue})
            fetcher.store(listing_url, {"cards": page_cards})

        if not page_cards:
            break

        for c in page_cards:
            href = c["href"]
            if href in seen_hrefs or href.startswith("/sydney-symphony-orchestra"):
                continue
            seen_hrefs.add(href)
            full = href if href.startswith("http") else f"{base}{href}"
            cards.append((c["title"], full, c["venue"]))

    if len(cards) > max_pages:
        print(f"  [WARNING] SOH event pages capped at {max_pages}; "
              f"{len(cards) - max_pages} not checked. Raise max_event_pages.")
        cards = cards[:max_pages]

    performances: List[Performance] = []
    for title, url, venue_name in cards:
        cached = fetcher.cached(url, fetcher.tier_for_event(None))
        if cached is not None:
            result = cached
        else:
            html = fetcher.get(url)
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(" ", strip=True)

            dts = extract_explicit_datetimes(text)
            if not dts:
                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        data = json.loads(script.string)
                    except (ValueError, TypeError):
                        continue
                    graph = data.get("@graph", [data]) if isinstance(data, dict) else []
                    for node in graph:
                        if isinstance(node, dict) and node.get("@type") == "Event":
                            dt = parse_iso_datetime(node.get("startDate", ""), assume_utc=True)
                            if dt:
                                dts.append(dt)
            result = {"datetimes": [d.isoformat() for d in dts]}
            fetcher.store(url, result)

        dts = [datetime.fromisoformat(s) for s in result.get("datetimes", [])]
        if not dts:
            continue

        # Presenter from the URL path, e.g. /australian-chamber-orchestra/...
        parts = [p for p in url.replace(base, "").split("/") if p]
        presenter = "Sydney Opera House"
        if parts and parts[0] not in ("whats-on", "classical-music", "events"):
            presenter = parts[0].replace("-", " ").title()

        soh_halls = ("concert hall", "utzon room", "joan sutherland theatre",
                     "drama theatre", "playhouse", "studio", "sydney opera house")
        display_venue = (f"{venue_name}, Sydney Opera House"
                         if venue_name.lower() in soh_halls
                         and "opera house" not in venue_name.lower() else venue_name)

        for dt in dts:
            performances.append(Performance(
                performer=presenter,
                title=title,
                date=dt,
                venue_name=display_venue,
                venue_address=lookup_venue_address(display_venue),
                url=url,
                source="Sydney Opera House",
            ))

    return performances


def scrape_willoughby_symphony(fetcher: Fetcher) -> List[Performance]:
    """
    Willoughby Symphony Orchestra. Their own site often omits performance
    times; The Concourse (their home venue) publishes them exactly, and the
    merge in core.suppress_unconfirmed_duplicates prefers the timed entry.
    Times are never invented here.
    """
    base = "https://www.willoughbysymphony.com.au"
    events_url = f"{base}/Events"

    cached = fetcher.cached(events_url, "listing")
    if cached is not None:
        links = cached.get("links", [])
    else:
        html = fetcher.get(events_url)
        soup = BeautifulSoup(html, "html.parser")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/Events/" in href and href.rstrip("/") != "/Events":
                full = href if href.startswith("http") else f"{base}{href}"
                if full not in links:
                    links.append(full)
        fetcher.store(events_url, {"links": links})

    performances: List[Performance] = []
    for url in links[:fetcher.config.get("max_event_pages", 200)]:
        cached = fetcher.cached(url, fetcher.tier_for_event(None))
        if cached is not None:
            result = cached
        else:
            html = fetcher.get(url)
            soup = BeautifulSoup(html, "html.parser")
            heading = soup.find(["h1", "h2"])
            title = heading.get_text(strip=True) if heading else ""
            text = soup.get_text(" ", strip=True)

            dts = extract_explicit_datetimes(text)
            if dts:
                result = {"title": title, "confirmed": True,
                          "datetimes": [d.isoformat() for d in dts]}
            else:
                dates = []
                for day, month, year in DATE_ONLY_PATTERN.findall(text)[:2]:
                    dt = parse_date_flexible(f"{day} {month} {year}")
                    if dt:
                        dates.append(dt.isoformat())
                result = {"title": title, "confirmed": False, "datetimes": dates}
            fetcher.store(url, result)

        title = result.get("title", "")
        if not title:
            continue
        for iso in result.get("datetimes", []):
            performances.append(Performance(
                performer="Willoughby Symphony Orchestra",
                title=title,
                date=datetime.fromisoformat(iso),
                venue_name="The Concourse Concert Hall",
                venue_address=lookup_venue_address("the concourse"),
                url=url,
                source="Willoughby Symphony",
                time_confirmed=result.get("confirmed", False),
            ))

    return performances


def scrape_the_concourse(fetcher: Fetcher) -> List[Performance]:
    """
    The Concourse, Chatswood. Both an additional source (Ku-ring-gai
    Philharmonic, Sydney Mozart Society, Live at Lunch) and the crosscheck
    that supplies exact times for Willoughby Symphony's own listings.
    """
    base = "https://www.theconcourse.com.au"
    listing_url = f"{base}/genre/music-classical/"

    cached = fetcher.cached(listing_url, "listing")
    if cached is not None:
        links = cached.get("links", [])
    else:
        html = fetcher.get(listing_url)
        soup = BeautifulSoup(html, "html.parser")
        links = []
        for a in soup.find_all("a", href=True):
            if "/event/" in a["href"]:
                full = a["href"] if a["href"].startswith("http") else f"{base}{a['href']}"
                if full not in links:
                    links.append(full)
        fetcher.store(listing_url, {"links": links})

    performances: List[Performance] = []
    for url in links[:fetcher.config.get("max_event_pages", 200)]:
        cached = fetcher.cached(url, fetcher.tier_for_event(None))
        if cached is not None:
            result = cached
        else:
            html = fetcher.get(url)
            soup = BeautifulSoup(html, "html.parser")
            heading = soup.find("h1")
            title = heading.get_text(strip=True) if heading else ""
            dts = extract_explicit_datetimes(soup.get_text(" ", strip=True))
            result = {"title": title, "datetimes": [d.isoformat() for d in dts]}
            fetcher.store(url, result)

        title = result.get("title", "")
        if not title:
            continue
        # Season-package pages aggregate every date in the season and would
        # duplicate each concert already listed individually.
        if re.search(r"subscription|season package", title, re.IGNORECASE):
            continue
        if title.isupper():
            title = title.title()

        performer = "Various artists"
        if ":" in title:
            prefix = title.split(":", 1)[0]
            if re.search(r"orchestra|symphony|philharmonia|society|choir|ensemble|quartet|band",
                         prefix, re.IGNORECASE):
                performer = prefix.strip()

        for iso in result.get("datetimes", []):
            performances.append(Performance(
                performer=performer,
                title=title,
                date=datetime.fromisoformat(iso),
                venue_name="The Concourse Concert Hall",
                venue_address=lookup_venue_address("the concourse"),
                url=url,
                source="The Concourse",
            ))

    return performances


def scrape_north_sydney_symphony(fetcher: Fetcher) -> List[Performance]:
    """
    North Sydney Symphony Orchestra.

    The site serves an empty HTML shell; the real content lives in
    JavaScript files on storage.googleapis.com referenced from that shell.
    Two formats appear: full headers ("SATURDAY 28TH MARCH 2026, 7.30pm")
    and a year-less season list ("19 Sep, 7.30pm - Verbrugghen Hall"). The
    year for the latter comes from the "2026 CONCERT DATES" heading in the
    same file, so an old season's leftovers can't become ghost events.
    """
    url = "https://www.nsso.org.au/concerts"

    cached = fetcher.cached(url, "listing")
    if cached is not None:
        content_urls = cached.get("content_urls", [])
    else:
        shell = fetcher.get(url)
        found = re.findall(
            r'https://storage\.googleapis\.com/te-websitebuilder-sites/[^\s"\'<>\\]+\.js[^\s"\'<>\\]*',
            shell)
        content_urls, seen_paths = [], set()
        for u in found:
            path = u.split("?")[0]
            if path not in seen_paths:
                seen_paths.add(path)
                content_urls.append(u)
        fetcher.store(url, {"content_urls": content_urls})

    def map_venue(hint: str) -> str:
        # Whitespace collapsed entirely: the source files sometimes break
        # words apart, e.g. "Verbrugghen Hal l".
        h = re.sub(r"[\s.]+", "", hint.lower())
        for needle, canonical in (
            ("verbrugghenhall", "Verbrugghen Hall"),
            ("stleonardspark", "St Leonards Park"),
            ("smithauditorium", "Smith Auditorium"),
            ("northsydneygirls", "North Sydney Girls High School"),
            ("nsghs", "North Sydney Girls High School"),
        ):
            if needle in h:
                return canonical
        return "See NSSO website"

    season_entry = re.compile(
        r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+"
        r"(\d{1,2})[.:](\d{2})\s*(am|pm)\s*[-–]\s*([A-Za-z.'’ ]{3,40})",
        re.IGNORECASE)
    full_entry = re.compile(
        rf"(?:{DAY_NAMES})\s+(\d{{1,2}})(?:ST|ND|RD|TH)?\s+({MONTH_NAMES})\s+(\d{{4}}),?\s+"
        rf"(\d{{1,2}})[.:](\d{{2}})\s*(am|pm)\s+(?:The\s+)?([A-Za-z][A-Za-z .,']{{3,60}})",
        re.IGNORECASE)

    def parse_content(text: str) -> List[dict]:
        """
        Extract concerts from one content file.

        Parsing happens before caching, so the cache holds a handful of
        dates rather than 190KB of page-builder scaffolding. An earlier
        version cached trimmed text instead and silently lost the year:
        the season list is written year-less ("19 Sep, 7.30pm") and takes
        its year from the "2026 CONCERT DATES" heading elsewhere in the
        same file.
        """
        found = []
        year_match = re.search(r"(\d{4})\s+CONCERT\s+DATES", text, re.IGNORECASE)
        if year_match:
            year = year_match.group(1)
            for day, month, hour, minute, ampm, venue_hint in season_entry.findall(text):
                dt = parse_date_flexible(f"{day} {month} {year}",
                                         f"{hour}:{minute} {ampm.upper()}")
                if dt:
                    found.append({"iso": dt.isoformat(), "venue": venue_hint.strip()})

        for day, month, year, hour, minute, ampm, venue_hint in full_entry.findall(text):
            dt = parse_date_flexible(f"{day} {month} {year}",
                                     f"{hour}:{minute} {ampm.upper()}")
            if dt:
                found.append({"iso": dt.isoformat(), "venue": venue_hint.strip()})
        return found

    collected: List[dict] = []
    for content_url in content_urls[:fetcher.config.get("max_event_pages", 200)]:
        cached = fetcher.cached(content_url, "static_content")
        if cached is not None:
            collected.extend(cached.get("events", []))
            continue
        raw = fetcher.get(content_url)
        raw = raw.replace('\\"', '"').replace("\\/", "/").replace("\\n", " ")
        raw = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), raw)
        text = re.sub(r"<[^>]+>", " ", raw)
        text = text.replace("&nbsp;", " ").replace("&quot;", '"').replace("&amp;", "&")
        events = parse_content(text)
        fetcher.store(content_url, {"events": events})
        collected.extend(events)

    performances: List[Performance] = []
    seen = set()
    for ev in collected:
        dt = datetime.fromisoformat(ev["iso"])
        if dt in seen:
            continue
        seen.add(dt)
        venue = map_venue(ev.get("venue", ""))
        performances.append(Performance(
            performer="North Sydney Symphony Orchestra",
            title="North Sydney Symphony Orchestra in Concert",
            date=dt,
            venue_name=venue,
            venue_address=lookup_venue_address(venue, "North Sydney area"),
            url=url,
            source="North Sydney Symphony",
        ))

    return performances


SCRAPERS: List[Tuple[str, Callable[[Fetcher], List[Performance]]]] = [
    ("Sydney Symphony Orchestra", scrape_sydney_symphony),
    ("Sydney Opera House", scrape_sydney_opera_house),
    ("Willoughby Symphony", scrape_willoughby_symphony),
    ("The Concourse", scrape_the_concourse),
    ("North Sydney Symphony", scrape_north_sydney_symphony),
]
