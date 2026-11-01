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
    programme_pages = 0
    programme_failures = 0
    for event in events:
        title = event["title"]
        slug = event.get("slug", "")
        url = (f"https://www.sydneysymphony.com/events/{slug}" if slug
               else "https://www.sydneysymphony.com/concert-tickets/whats-on")
        venue_name = event.get("venue") or "Concert Hall, Sydney Opera House"

        # The API carries no repertoire, but each event page has a structured
        # programme panel. Only upcoming concerts are worth a request, and a
        # run of failures stops further attempts rather than hammering a
        # site that is blocking or down - programmes are a nice-to-have.
        works: List[dict] = []
        upcoming = sorted(d for d in (parse_iso_datetime(s) for s in event.get("starts", []))
                          if d and d >= now_sydney())
        if slug and upcoming and programme_failures < 3 \
                and programme_pages < fetcher.config.get("max_event_pages", 200):
            programme_pages += 1
            works, ok = sso_programme(fetcher, url, upcoming[0])
            programme_failures = 0 if ok else programme_failures + 1

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
                works=works,
            ))

    return performances


def sso_programme(fetcher: Fetcher, url: str, first_date: datetime) -> Tuple[List[dict], bool]:
    """
    Composer/work pairs from an SSO event page's programme panel, e.g.
    <h3>TCHAIKOVSKY</h3><p>Violin Concerto</p>. Names are cached as written;
    core.classify() normalises them, so a config change needs no refetch.

    Returns (works, ok). A failed fetch keeps the last good extraction rather
    than overwriting it with nothing.
    """
    cached = fetcher.cached(url, fetcher.tier_for_event(first_date))
    if cached is not None:
        return cached.get("works", []), True
    previous = fetcher.cache.get(canonical_url(url)) or {}
    if fetcher.offline:
        return (previous.get("result") or {}).get("works", []), True

    try:
        html = fetcher.get(url)
    except SourceError:
        if previous.get("ok"):
            return previous["result"].get("works", []), False
        fetcher.store(url, {"works": []}, ok=False)
        return [], False

    soup = BeautifulSoup(html, "html.parser")
    works = []
    for item in soup.select(".program-panel .program-item__content"):
        name = item.select_one(".program-item__title")
        piece = item.select_one(".program-item__subtitle")
        composer = name.get_text(" ", strip=True) if name else ""
        if composer:
            works.append({"composer": composer,
                          "work": piece.get_text(" ", strip=True) if piece else ""})
    fetcher.store(url, {"works": works})
    return works, True


def _soh_listing_cards(fetcher: Fetcher, listing_url: str) -> List[dict]:
    """One page of an Opera House genre listing, as {href, title, venue} cards."""
    cached = fetcher.cached(listing_url, "listing")
    if cached is not None:
        return cached.get("cards", [])
    soup = BeautifulSoup(fetcher.get(listing_url), "html.parser")
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
    return page_cards


def scrape_sydney_opera_house(fetcher: Fetcher) -> List[Performance]:
    """
    Sydney Opera House classical and opera listings (genres from config).

    The listing pages are server-rendered Drupal (div.card--event). Events
    presented by the SSO are skipped because the SSO API already supplies
    them with authoritative times. For the rest, each event page carries a
    "Dates and times" table; JSON-LD is the fallback, and its startDate is
    UTC with no timezone marker. A genre with a "format" (opera) tags its
    events so the Event type filter can show or hide them.
    """
    base = "https://www.sydneyoperahouse.com"
    max_pages = fetcher.config.get("max_event_pages", 200)
    genres = fetcher.config.get("sydney_opera_house_genres",
                                [{"id": "1436", "label": "Classical Music"}])
    cards: List[Tuple[str, str, str]] = []
    hints: Dict[str, List[str]] = {}     # full URL -> format hints
    seen_hrefs = set()

    for genre in genres:
        for page in range(0, 8):
            listing_url = f"{base}/whats-on?genre%5B%5D={genre['id']}&page={page}"
            page_cards = _soh_listing_cards(fetcher, listing_url)
            if not page_cards:
                break

            for c in page_cards:
                href = c["href"]
                full = href if href.startswith("http") else f"{base}{href}"
                if genre.get("format"):
                    hints.setdefault(full, []).append(genre["format"])
                if href in seen_hrefs or href.startswith("/sydney-symphony-orchestra"):
                    continue
                seen_hrefs.add(href)
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
                format_hints=hints.get(url, []),
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


def scrape_city_recital_hall(fetcher: Fetcher) -> List[Performance]:
    """
    City Recital Hall, Angel Place, via the JSON endpoint behind its What's On
    page. A mixed venue - author talks and pop gigs share the stage - but every
    event carries the hall's own genre, so classical is the venue's call, not
    ours. Opera is kept: here it is Pinchgut's baroque repertoire.

    Times are "YYYYMMDDHHMM" in Sydney local time, one per performance, and
    the programme is a <dt>composer</dt><dd>work</dd> list. Sydney Symphony
    "Spotlight" concerts held here are skipped because the SSO API is
    authoritative for them.
    """
    base = "https://www.cityrecitalhall.com"
    api_url = f"{base}/umbraco/Api/SearchApi/WhatsOn?range=upcoming&page=1&pageSize=200"
    genres = fetcher.config.get("city_recital_hall_genres", ["Classical Music", "Opera"])

    cached = fetcher.cached(api_url, "listing")
    # Extractions cached before genres were kept lack the key; refetch.
    if cached is not None and all("genres" in ev for ev in cached.get("events", [])):
        events = cached.get("events", [])
    else:
        data = fetcher.get(api_url, as_json=True)
        paging = data.get("paging") or {}
        if paging.get("hasNextPage"):
            print(f"  [WARNING] City Recital Hall lists {paging.get('totalItems')} events; "
                  f"only the first page was read. Raise pageSize.")
        events = []
        for item in data.get("items", []):
            v = item.get("values") or {}
            if v.get("hideFromWhatsOn") == "1" or not v.get("nodeName"):
                continue
            event_genres = [g.strip() for g in (v.get("genre") or "").split(",")]
            if not any(g in genres for g in event_genres):
                continue
            works = []
            program = BeautifulSoup(v.get("__Raw_programDetail") or "", "html.parser")
            for dt_tag in program.find_all("dt"):
                dd = dt_tag.find_next_sibling("dd")
                works.append({"composer": dt_tag.get_text(" ", strip=True),
                              "work": dd.get_text(" ", strip=True) if dd else ""})
            events.append({
                "title": v["nodeName"].strip(),
                "presenter": (v.get("presenter") or "").strip(),
                "path": v.get("url", ""),
                "starts": (v.get("eventShowDateTimes") or "").split(),
                "genres": event_genres,
                "works": works,
            })
        fetcher.store(api_url, {"events": events})

    performances: List[Performance] = []
    for ev in events:
        if "sydney symphony" in ev["presenter"].lower():
            continue
        for stamp in ev["starts"]:
            try:
                dt = datetime.strptime(stamp, "%Y%m%d%H%M")
            except ValueError:
                continue
            performances.append(Performance(
                performer=ev["presenter"] or "Various artists",
                title=ev["title"],
                date=dt,
                venue_name="City Recital Hall",
                venue_address=lookup_venue_address("city recital hall"),
                url=f"{base}{ev['path']}" if ev["path"].startswith("/") else ev["path"] or base,
                source="City Recital Hall",
                works=ev["works"],
                format_hints=["opera"] if any("opera" in g.lower() for g in ev["genres"]) else [],
            ))
    return performances


# "Saturday 7 November 2026 from 5pm to 6pm". The year is omitted for dates
# in the current year ("Wednesday 4 November from 1:15pm").
_COS_DATE_LINE = re.compile(
    rf"({DAY_NAMES})\s+(\d{{1,2}})\s+({MONTH_NAMES})(?:\s+(\d{{4}}))?"
    r"(?:\s+from\s+(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm))?",
    re.IGNORECASE)


def _date_for_weekday(weekday: str, day: str, month: str, year: str,
                      prefer: Optional[int] = None) -> Optional[datetime]:
    """
    Resolve a date that may lack its year. The weekday decides: 7 February is
    a Saturday in 2026 and a Sunday in 2027, so a year-less "Saturday 7
    February" read in November is February 2026 (past), never a phantom 2027
    concert. `prefer` (e.g. a season's year) is tried first. No nearby year
    fitting the weekday means the line is dropped.
    """
    if year:
        return parse_date_flexible(f"{day} {month} {year}")
    this_year = now_sydney().year
    candidates = ([prefer] if prefer else []) + [this_year, this_year + 1, this_year - 1]
    for y in candidates:
        dt = parse_date_flexible(f"{day} {month} {y}")
        if dt and dt.strftime("%A").lower() == weekday.lower():
            return dt
    return None


def scrape_city_of_sydney(fetcher: Fetcher) -> List[Performance]:
    """
    City of Sydney's What's On guide: an aggregator of organiser-submitted
    events, which is how St James' King St, The Neilson, the Conservatorium
    and smaller halls get in without a scraper each.

    Classical is the ORGANISER's tag, not ours, and is trusted - but talks,
    lectures and films that carry it are dropped. Venues another source
    already covers are skipped by name and street address, because this
    guide spells them differently and the dedup would see two concerts.

    The listing gives only the next performance's time, so each event page is
    read for "datesHumanised", which has every date with its time. A date with
    no time stays "Time TBA"; nothing is assumed.
    """
    base = "https://whatson.cityofsydney.nsw.gov.au"
    listing_url = f"{base}/categories/music"
    cfg = fetcher.config.get("city_of_sydney", {})
    skip_venues = [s.lower() for s in cfg.get("skip_venues", [])]
    skip_tags = set(cfg.get("skip_tags", []))
    skip_categories = set(cfg.get("skip_categories", []))

    def covered(*texts: str) -> bool:
        joined = " ".join(t for t in texts if t).lower()
        return any(s in joined for s in skip_venues)

    cached = fetcher.cached(listing_url, "listing")
    if cached is not None:
        events = cached.get("events", [])
    else:
        html = fetcher.get(listing_url)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            raise SourceError("City of Sydney: no __NEXT_DATA__ on listing page", "broken")
        listing = json.loads(m.group(1))["props"]["pageProps"]["events"]
        if listing.get("nbHits", 0) > len(listing.get("hits", [])):
            print(f"  [WARNING] City of Sydney lists {listing['nbHits']} music events; "
                  f"only {len(listing['hits'])} were returned.")
        events = [{"slug": h["slug"], "venue": h.get("venueName") or "",
                   "next": h.get("upcomingDate"),
                   "opera": "opera" in (h.get("tags") or [])}
                  for h in listing.get("hits", [])
                  if "classical" in (h.get("tags") or [])
                  and not skip_tags & set(h.get("tags") or [])
                  and not skip_categories & set(h.get("categories") or [])
                  and h.get("slug")]
        fetcher.store(listing_url, {"events": events})

    performances: List[Performance] = []
    for ev in events[:fetcher.config.get("max_event_pages", 200)]:
        if covered(ev["venue"]):
            continue
        url = f"{base}/events/{ev['slug']}"
        nxt = parse_date_flexible(ev.get("next") or "")
        cached = fetcher.cached(url, fetcher.tier_for_event(nxt))
        # Extractions cached before organisers were read lack the key; refetch.
        if cached is not None and "organiser" in cached:
            result = cached
        else:
            html = fetcher.get(url)
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
            data = (json.loads(m.group(1))["props"]["pageProps"].get("eventData") or {}) if m else {}
            venue = data.get("eventVenue") or {}
            suburb = data.get("eventSuburb") or {}
            dates = []
            for weekday, day, month, year, hour, minute, ampm in \
                    _COS_DATE_LINE.findall(data.get("datesHumanised") or ""):
                dt = _date_for_weekday(weekday, day, month, year)
                if not dt:
                    continue
                if hour:
                    h = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
                    dt = dt.replace(hour=h, minute=int(minute or 0))
                dates.append({"iso": dt.isoformat(), "timed": bool(hour)})
            result = {
                "title": (data.get("name") or "").strip(),
                "venue": venue.get("name") or suburb.get("name") or "",
                "address": venue.get("addressText") or data.get("addressText") or "",
                "organiser": (data.get("eventOrganiser") or "").strip(),
                "dates": dates,
            }
            fetcher.store(url, result)

        if not result.get("title") or covered(result.get("venue"), result.get("address")):
            continue
        venue_name = result.get("venue") or "See event page"
        # The organiser is usually the ensemble, but sometimes the hosting
        # church, a street address or a contact person - not a performer.
        organiser = re.sub(r"\s*\([^)]*\)\s*$", "", result.get("organiser") or "").strip()
        place = f"{venue_name} {result.get('address', '')}".lower()
        if not organiser or organiser.lower() in place \
                or re.search(r"\b(church|cathedral|chapel)\b", organiser, re.IGNORECASE):
            organiser = "Various artists"
        for d in result.get("dates", []):
            performances.append(Performance(
                performer=organiser,
                title=result["title"],
                date=datetime.fromisoformat(d["iso"]),
                venue_name=venue_name,
                venue_address=result.get("address") or lookup_venue_address(venue_name),
                url=url,
                source="City of Sydney What's On",
                time_confirmed=d["timed"],
                format_hints=["opera"] if ev.get("opera") else [],
            ))
    return performances


# "Sydney: Sunday 15 November, 3.00pm Verbrugghen Hall, Conservatorium of Music"
_SALUT_SYDNEY_LINE = re.compile(
    rf"^Sydney\s*:\s*(?:({DAY_NAMES}),?\s+)?(\d{{1,2}})\s+({MONTH_NAMES}),?(?:\s+(\d{{4}}),?)?"
    r"\s+(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\s*(.*)$",
    re.IGNORECASE)


def scrape_salut_baroque(fetcher: Fetcher) -> List[Performance]:
    """
    Salut! Baroque: a Canberra/Sydney period-instrument ensemble whose Sydney
    concerts are Sunday afternoons. The City of Sydney guide lists them with
    no programme; the ensemble's season page has every composer and work, so
    this source supplies them and the guide's copy is dropped as a duplicate.

    The season page moves each year (/2026-concert-series-power-passion-politics/),
    so it is found from /concerts/. Each concert is an <h3>Concert N: Title</h3>
    followed by a "Sydney: Sunday 15 November, 3.00pm <venue>" line and a
    programme written either "Composer – Work" or "<strong>Composer</strong> Work".
    Canberra dates are ignored.
    """
    base = "https://baroque.com.au"
    index_url = f"{base}/concerts/"

    cached = fetcher.cached(index_url, "listing")
    if cached is not None:
        season_urls = cached.get("seasons", [])
    else:
        soup = BeautifulSoup(fetcher.get(index_url), "html.parser")
        season_urls = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"/\d{4}-concert-series", href) and href not in season_urls:
                season_urls.append(href if href.startswith("http") else f"{base}{href}")
        fetcher.store(index_url, {"seasons": season_urls})

    performances: List[Performance] = []
    for season_url in season_urls[:3]:
        cached = fetcher.cached(season_url, "listing")
        if cached is not None:
            concerts = cached.get("concerts", [])
        else:
            concerts = _parse_salut_season(fetcher.get(season_url))
            fetcher.store(season_url, {"concerts": concerts})

        for c in concerts:
            performances.append(Performance(
                performer="Salut! Baroque",
                title=c["title"],
                date=datetime.fromisoformat(c["iso"]),
                venue_name=c["venue"],
                venue_address=lookup_venue_address(c["venue"]),
                url=season_url,
                source="Salut! Baroque",
                works=c["works"],
            ))
    return performances


def _parse_salut_season(html: str) -> List[dict]:
    """Extract each concert's Sydney performance and programme from a season page."""
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    season = re.search(r"(\d{4})", title_tag.get_text() if title_tag else "")
    prefer = int(season.group(1)) if season else None

    concerts = []
    headings = [h for h in soup.find_all("h3")
                if re.match(r"\s*Concert\s+\d+\s*:", h.get_text())]
    for heading in headings:
        title = re.sub(r"^\s*Concert\s+\d+\s*:\s*", "", heading.get_text(" ", strip=True))
        paragraphs = []
        for el in heading.find_all_next(["p", "h3"]):
            if el.name == "h3":
                if el in headings:
                    break
                continue
            paragraphs.append(el)

        when, works = None, []
        for i, p in enumerate(paragraphs):
            text = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).replace(" ,", ",")
            m = _SALUT_SYDNEY_LINE.match(text)
            if m and not when:
                weekday, day, month, year, hour, minute, ampm, venue = m.groups()
                dt = (_date_for_weekday(weekday, day, month, year, prefer) if weekday
                      else parse_date_flexible(f"{day} {month} {year or prefer}"))
                if dt:
                    h = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
                    when = (dt.replace(hour=h, minute=int(minute or 0)),
                            venue.strip(" ,") or "See Salut! Baroque website")
            if re.match(r"^Program\b", text) and i + 1 < len(paragraphs) and not works:
                works = _salut_programme(paragraphs[i + 1])

        if when:
            concerts.append({"title": title, "iso": when[0].isoformat(),
                             "venue": when[1], "works": works})
    return concerts


def _salut_programme(paragraph) -> List[dict]:
    """One work per <br>-separated line: 'Composer – Work' or '<strong>Composer</strong> Work'."""
    works = []
    for fragment in re.split(r"<br\s*/?>", str(paragraph)):
        line = BeautifulSoup(fragment, "html.parser")
        text = re.sub(r"\s+", " ", line.get_text(" ", strip=True)).strip()
        text = re.sub(r"\s+([,;\]])", r"\1", text).replace("[ ", "[")
        if not text:
            continue
        parts = re.split(r"\s+[–—-]\s+", text, maxsplit=1)
        if len(parts) == 2:
            composer, work = parts
        else:
            strong = line.find("strong")
            if not strong:
                continue
            composer = strong.get_text(" ", strip=True)
            work = text[len(composer):] if text.startswith(composer) else text
        works.append({"composer": composer.strip(), "work": work.strip(" –—-")})
    return works


SCRAPERS: List[Tuple[str, Callable[[Fetcher], List[Performance]]]] = [
    ("Sydney Symphony Orchestra", scrape_sydney_symphony),
    ("Sydney Opera House", scrape_sydney_opera_house),
    ("Willoughby Symphony", scrape_willoughby_symphony),
    ("The Concourse", scrape_the_concourse),
    ("North Sydney Symphony", scrape_north_sydney_symphony),
    ("City Recital Hall", scrape_city_recital_hall),
    ("City of Sydney What's On", scrape_city_of_sydney),
    ("Salut! Baroque", scrape_salut_baroque),
]
