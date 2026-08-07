#!/usr/bin/env python3
"""
Sydney Matinee Music Finder v2.0

This script scrapes upcoming classical and community orchestra performances in Sydney,
with a special focus on matinee shows (performances starting before 5:00 PM).

Data Sources:
- Sydney Symphony Orchestra (JSON API endpoint)
- Sydney Opera House (What's On - Classical Music listing + event pages)
- Willoughby Symphony Orchestra (Events page + individual event pages)
- The Concourse, Chatswood (Classical Music genre listing + event pages)
- North Sydney Symphony Orchestra (Concerts page content files)

Output: A self-contained HTML file (sydney_matinees.html) that can be opened in any browser.

Notes on data quality:
- All times are converted to Sydney local time using the zoneinfo database, which
  handles daylight-saving transitions exactly (requires the tzdata package on Windows).
- Events whose start time could not be found are shown as "Time TBA" and are NEVER
  marked as matinees - no times are fabricated.
- The Concourse listing doubles as a crosscheck for Willoughby Symphony: duplicates
  of the same performance found on both sites are merged.

Security Note: This script only connects to the five target websites listed above.
It performs read-only operations and writes only the output HTML file.
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
import time
import re
from typing import List, Dict, Optional
from dataclasses import dataclass, field
import json

try:
    from zoneinfo import ZoneInfo
    SYDNEY_TZ = ZoneInfo("Australia/Sydney")
except Exception:
    # zoneinfo lookup fails on Windows when the tzdata package is missing.
    # A manual first-Sunday DST fallback is used in to_sydney_time() below.
    SYDNEY_TZ = None

# =============================================================================
# CONFIGURATION
# =============================================================================

# Custom User-Agent to identify ourselves politely
USER_AGENT = "SydneyMatineeFinder/2.0 (Bot for personal, non-commercial use)"

# Delay between requests to be polite to servers (in seconds)
REQUEST_DELAY = 2.0

# Matinee cutoff time (performances before this time are considered matinees)
MATINEE_CUTOFF_HOUR = 17  # 5:00 PM

# Request timeout (seconds)
REQUEST_TIMEOUT = 30

# Cap on individual event pages visited per source (politeness / runtime)
MAX_EVENT_PAGES = 40

# Headers for all requests
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

# Headers for JSON API requests
JSON_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "en-AU,en;q=0.9",
}

MONTH_NAMES = ("January|February|March|April|May|June|July|August|September|"
               "October|November|December")
DAY_NAMES = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"

# Explicit "Saturday, 08 August 2026 07:00 PM" style pattern.
# Requiring date-before-time avoids matching on-sale notices like
# "9am, Wednesday 26 November 2025".
EXPLICIT_DATETIME_PATTERN = re.compile(
    rf'(?:{DAY_NAMES}),?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_NAMES})\s+(\d{{4}}),?\s+'
    rf'(\d{{1,2}})(?:[:.](\d{{2}}))?\s*(AM|PM)',
    re.IGNORECASE
)

# Date-only pattern, e.g. "21 March 2026"
DATE_ONLY_PATTERN = re.compile(
    rf'(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_NAMES})\s+(\d{{4}})',
    re.IGNORECASE
)

# Well-known venue addresses
VENUE_ADDRESSES = {
    'concert hall': 'Bennelong Point, Sydney NSW 2000',
    'utzon room': 'Bennelong Point, Sydney NSW 2000',
    'joan sutherland theatre': 'Bennelong Point, Sydney NSW 2000',
    'drama theatre': 'Bennelong Point, Sydney NSW 2000',
    'playhouse': 'Bennelong Point, Sydney NSW 2000',
    'studio': 'Bennelong Point, Sydney NSW 2000',
    'sydney opera house': 'Bennelong Point, Sydney NSW 2000',
    'sydney town hall': '483 George St, Sydney NSW 2000',
    'city recital hall': '2 Angel Pl, Sydney NSW 2000',
    "st james' church": "173 King St, Sydney NSW 2000",
    "st james'": "173 King St, Sydney NSW 2000",
    'state theatre': '49 Market St, Sydney NSW 2000',
    'the concourse': '409 Victoria Ave, Chatswood NSW 2067',
    'verbrugghen hall': 'Sydney Conservatorium of Music, Macquarie St, Sydney NSW 2000',
    'st leonards park': 'Miller St, North Sydney NSW 2060',
    'smith auditorium': 'Shore School, Blue St, North Sydney NSW 2060',
    'north sydney girls': '365 Pacific Hwy, Crows Nest NSW 2065',
    "st philip's church": '3 York St, Sydney NSW 2000',
}


def lookup_venue_address(venue_name: str, default: str = 'Sydney, NSW') -> str:
    """Map a venue name to a street address if we know it."""
    lowered = venue_name.lower()
    for key, addr in VENUE_ADDRESSES.items():
        if key in lowered:
            return addr
    return default


def normalize_title(title: str) -> str:
    """
    Normalize a concert title for cross-source matching: lowercase,
    alphanumerics only, standalone years removed ("Last Night of the Proms
    2026" and "Last Night Of The Proms" must compare equal).
    """
    norm = re.sub(r'[^a-z0-9]+', ' ', title.lower())
    norm = re.sub(r'\b(?:19|20)\d{2}\b', '', norm)
    return re.sub(r'\s+', ' ', norm).strip()


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class Performance:
    """Represents a single performance/concert."""
    performer: str           # Orchestra or performer name
    title: str               # Concert title
    date: datetime           # Date and time of performance (naive, Sydney local)
    venue_name: str          # Name of the venue
    venue_address: str       # Address of the venue
    url: str                 # Direct link to booking/info page
    source: str              # Which website this came from
    time_confirmed: bool = True  # False when only the date (not the time) is known

    @property
    def is_matinee(self) -> bool:
        """
        Check if this performance is a matinee (before 5 PM).
        Events with unknown/unconfirmed start times are never marked as matinees.
        """
        if not self.time_confirmed:
            return False
        if self.date.hour == 0 and self.date.minute == 0:
            return False  # midnight placeholder = time unknown
        return self.date.hour < MATINEE_CUTOFF_HOUR

    @property
    def time_str(self) -> str:
        """Return formatted time string."""
        if not self.time_confirmed or (self.date.hour == 0 and self.date.minute == 0):
            return "Time TBA"
        return self.date.strftime("%I:%M %p").lstrip("0")

    @property
    def date_str(self) -> str:
        """Return formatted date string."""
        return self.date.strftime("%A, %d %B %Y")

    def unique_key(self) -> str:
        """
        Generate a unique key for deduplication.
        Normalization is aggressive (alphanumerics only, years stripped) so
        the same concert listed on two websites - e.g. Willoughby Symphony's
        own site and The Concourse - merges despite curly quotes, casing, or
        "... 2026" suffix differences. The performance date carries the year.
        """
        title_norm = normalize_title(self.title)
        date_norm = self.date.strftime("%Y-%m-%d %H:%M")
        venue_norm = re.sub(r'[^a-z0-9]+', ' ', self.venue_name.lower()).strip()
        return f"{title_norm}|{date_norm}|{venue_norm}"


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def make_request(url: str, session: requests.Session, headers: dict = None) -> Optional[requests.Response]:
    """
    Make a polite HTTP request with error handling.
    Returns None if the request fails.
    """
    if headers is None:
        headers = HEADERS
    try:
        response = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response
    except requests.RequestException as e:
        print(f"  [ERROR] Failed to fetch {url}: {e}")
        return None


def _first_sunday(year: int, month: int) -> int:
    """Day-of-month of the first Sunday in the given month."""
    d = datetime(year, month, 1)
    return 1 + (6 - d.weekday()) % 7


def to_sydney_time(dt_aware: datetime) -> datetime:
    """
    Convert an aware datetime to naive Sydney local time.
    Uses zoneinfo when available; otherwise falls back to the actual
    Australian DST rule (AEDT from first Sunday of October to first
    Sunday of April).
    """
    if SYDNEY_TZ is not None:
        return dt_aware.astimezone(SYDNEY_TZ).replace(tzinfo=None)

    # Manual fallback: tentatively convert with standard time (UTC+10),
    # then check whether that local date falls inside the DST window.
    local = (dt_aware.astimezone(timezone.utc) + timedelta(hours=10)).replace(tzinfo=None)
    m, y = local.month, local.year
    in_dst = (
        m > 10 or m < 4
        or (m == 10 and local.day >= _first_sunday(y, 10))
        or (m == 4 and local.day < _first_sunday(y, 4))
    )
    return local + timedelta(hours=1) if in_dst else local


def parse_iso_datetime(iso_str: str, assume_utc: bool = False) -> Optional[datetime]:
    """
    Parse an ISO 8601 datetime string and return naive Sydney local time.
    Handles formats like: 2026-02-25T19:30:00.000+11:00 or 2026-01-30T07:00:00.000Z

    - Strings with an explicit offset (or Z) are converted to Sydney time.
    - Naive strings are returned as-is, unless assume_utc=True, in which case
      they are treated as UTC and converted (the Sydney Opera House embeds
      UTC times without any timezone marker).
    """
    if not iso_str:
        return None

    s = iso_str.strip().replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Fallback: strip milliseconds/offset and try the plain format
        clean = re.sub(r'\.\d+', '', iso_str.strip()).rstrip('Zz')
        clean = re.sub(r'[+-]\d{2}:?\d{2}$', '', clean)
        try:
            dt = datetime.strptime(clean[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None

    if dt.tzinfo is None:
        if assume_utc:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            return dt

    return to_sydney_time(dt)


def parse_date_flexible(date_str: str, time_str: str = None) -> Optional[datetime]:
    """
    Try to parse a date string with various formats.
    Returns None if parsing fails.
    """
    if not date_str:
        return None

    # Clean up the strings
    date_str = date_str.strip()
    time_str = time_str.strip() if time_str else ""

    # Common date formats to try
    date_formats = [
        "%d %B %Y",      # 25 January 2026
        "%d %b %Y",      # 25 Jan 2026
        "%B %d, %Y",     # January 25, 2026
        "%d/%m/%Y",      # 25/01/2026
        "%Y-%m-%d",      # 2026-01-25
        "%A %d %B %Y",   # Saturday 25 January 2026
        "%A, %d %B %Y",  # Saturday, 25 January 2026
        "%a %d %b %Y",   # Sat 25 Jan 2026
        "%d %B",         # 25 January (assume current/next year)
        "%d %b",         # 25 Jan
    ]

    # Time formats to try
    time_formats = [
        "%I:%M %p",      # 2:00 PM
        "%I:%M%p",       # 2:00PM
        "%H:%M",         # 14:00
        "%I %p",         # 2 PM
        "%I%p",          # 2PM
    ]

    parsed_date = None
    parsed_time = None

    # Try to parse the date
    for fmt in date_formats:
        try:
            parsed_date = datetime.strptime(date_str, fmt)
            # Handle year-less formats
            if parsed_date.year == 1900:
                current_year = datetime.now().year
                parsed_date = parsed_date.replace(year=current_year)
                # If the date has passed, assume next year
                if parsed_date < datetime.now():
                    parsed_date = parsed_date.replace(year=current_year + 1)
            break
        except ValueError:
            continue

    if not parsed_date:
        return None

    # Try to parse the time if provided
    if time_str:
        # Clean up time string - normalize to consistent format
        time_clean = time_str.upper().strip()
        time_clean = re.sub(r'\s+', ' ', time_clean)  # Normalize spaces
        time_clean = time_clean.replace('.', ':')      # 2.00 -> 2:00

        # Try each format
        for fmt in time_formats:
            try:
                parsed_time = datetime.strptime(time_clean, fmt)
                parsed_date = parsed_date.replace(
                    hour=parsed_time.hour,
                    minute=parsed_time.minute
                )
                break
            except ValueError:
                continue

    return parsed_date


def extract_explicit_datetimes(text: str) -> List[datetime]:
    """
    Find all "Saturday, 08 August 2026 07:00 PM" style date+times in a blob
    of text. Returns parsed datetimes (deduplicated, order preserved).
    """
    results = []
    seen = set()
    for m in EXPLICIT_DATETIME_PATTERN.finditer(text):
        day, month, year, hour, minute, ampm = m.groups()
        minute = minute or "00"
        dt = parse_date_flexible(f"{day} {month} {year}", f"{hour}:{minute} {ampm.upper()}")
        if dt and dt not in seen:
            seen.add(dt)
            results.append(dt)
    return results


def polite_delay():
    """Wait between requests to be polite to servers."""
    time.sleep(REQUEST_DELAY)


# =============================================================================
# SCRAPER FUNCTIONS
# =============================================================================

def scrape_sydney_symphony_api(session: requests.Session) -> List[Performance]:
    """
    Scrape performances from Sydney Symphony Orchestra using their JSON API.
    This is more reliable than parsing HTML as it gives us structured data.
    The API returns UTC timestamps (Z suffix) which are converted to Sydney time.
    """
    print("Scraping Sydney Symphony Orchestra (API)...")
    performances = []

    api_url = "https://www.sydneysymphony.com/api/events"

    response = make_request(api_url, session, JSON_HEADERS)
    if not response:
        print("  Could not access Sydney Symphony API")
        return performances

    try:
        data = response.json()
        events = data.get('docs', [])

        seen_instances = set()  # Track unique event instances

        for event in events:
            try:
                # Skip non-dict entries (API may return mixed data)
                if not isinstance(event, dict):
                    continue

                title = event.get('title', '').strip()
                if not title:
                    continue

                slug = event.get('slug', '')
                event_url = f"https://www.sydneysymphony.com/events/{slug}" if slug else "https://www.sydneysymphony.com/concert-tickets/whats-on"

                # Get venue information
                venue_data = event.get('venue', {})
                if isinstance(venue_data, dict):
                    venue_name = venue_data.get('title', 'Sydney Opera House Concert Hall')
                else:
                    venue_name = 'Sydney Opera House Concert Hall'
                venue_address = lookup_venue_address(venue_name)

                # Get all event instances (individual performance dates/times)
                instances_data = event.get('eventInstances', {})
                if isinstance(instances_data, dict):
                    instances = instances_data.get('docs', [])
                elif isinstance(instances_data, list):
                    instances = instances_data
                else:
                    instances = []

                # Fall back to the event-level startDate if there are no instances
                if not instances and event.get('startDate'):
                    instances = [{'startDate': event['startDate']}]

                for instance in instances:
                    if not isinstance(instance, dict):
                        continue

                    start_date_str = instance.get('startDate', '')
                    if not start_date_str:
                        continue

                    perf_date = parse_iso_datetime(start_date_str)
                    if not perf_date:
                        continue

                    # Create unique key to avoid duplicates
                    instance_key = f"{title}|{perf_date.strftime('%Y-%m-%d %H:%M')}"
                    if instance_key in seen_instances:
                        continue
                    seen_instances.add(instance_key)

                    performances.append(Performance(
                        performer="Sydney Symphony Orchestra",
                        title=title,
                        date=perf_date,
                        venue_name=venue_name,
                        venue_address=venue_address,
                        url=event_url,
                        source="Sydney Symphony Orchestra"
                    ))

            except Exception as e:
                print(f"  [WARNING] Error parsing SSO event '{event.get('title', 'unknown')}': {e}")
                continue

    except json.JSONDecodeError as e:
        print(f"  [ERROR] Could not parse SSO API response: {e}")
        return performances

    print(f"  Found {len(performances)} events from Sydney Symphony Orchestra")
    return performances


def scrape_sydney_opera_house(session: requests.Session) -> List[Performance]:
    """
    Scrape classical music from the Sydney Opera House What's On listing.

    The listing pages are server-rendered Drupal (div.card--event cards).
    Events presented by the Sydney Symphony Orchestra are skipped because the
    SSO API already provides them with authoritative times. For the rest, each
    event page is visited to read the "Dates and times" table; if that is
    missing, the schema.org JSON-LD startDate is used (it is UTC without a
    timezone marker, so it is converted to Sydney time).
    """
    print("Scraping Sydney Opera House...")
    performances = []
    base_url = "https://www.sydneyoperahouse.com"

    # Collect event cards across the paginated listing (genre 1436 = Classical Music).
    # The site defaults the date range to the next 12 months - no hardcoded dates.
    event_cards = []  # (title, url, venue_name)
    seen_hrefs = set()
    for page in range(0, 8):
        url = f"{base_url}/whats-on?genre%5B%5D=1436&page={page}"
        response = make_request(url, session)
        if not response:
            break
        soup = BeautifulSoup(response.text, 'html.parser')
        cards = soup.select('div.card--event')
        if not cards:
            break

        for card in cards:
            link = card.select_one('a.card__link')
            if not link:
                continue
            href = link.get('href', '')
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            title = link.get_text(strip=True)
            if not title:
                continue

            # SSO-presented events are already covered via the SSO API
            if href.startswith('/sydney-symphony-orchestra'):
                continue

            # Venue is the last non-empty text line of the card
            lines = [t.strip() for t in card.get_text('\n').split('\n') if t.strip()]
            venue_name = lines[-1] if lines else 'Sydney Opera House'
            if 'streamline' in venue_name.lower():  # icon alt-text noise
                venue_name = 'Sydney Opera House'

            full_url = href if href.startswith('http') else f"{base_url}{href}"
            event_cards.append((title, full_url, venue_name))

        polite_delay()

    print(f"  Found {len(event_cards)} non-SSO event pages to check...")

    for title, event_url, venue_name in event_cards[:MAX_EVENT_PAGES]:
        polite_delay()
        response = make_request(event_url, session)
        if not response:
            continue

        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text(' ', strip=True)

        # Primary: the "Dates and times" table rendered on every event page
        dates = extract_explicit_datetimes(page_text)
        time_confirmed = True

        # Fallback: schema.org JSON-LD (startDate is UTC, unmarked)
        if not dates:
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    data = json.loads(script.string)
                except (ValueError, TypeError):
                    continue
                graph = data.get('@graph', [data]) if isinstance(data, dict) else []
                for node in graph:
                    if isinstance(node, dict) and node.get('@type') == 'Event':
                        dt = parse_iso_datetime(node.get('startDate', ''), assume_utc=True)
                        if dt:
                            dates.append(dt)

        if not dates:
            continue

        # Presenter: derive from the URL path, e.g.
        # /australian-chamber-orchestra/... -> Australian Chamber Orchestra
        path_parts = [p for p in event_url.replace(base_url, '').split('/') if p]
        presenter = 'Sydney Opera House'
        if path_parts and path_parts[0] not in ('whats-on', 'classical-music', 'events'):
            presenter = path_parts[0].replace('-', ' ').title()

        if 'opera house' not in venue_name.lower():
            venue_display = f"{venue_name}, Sydney Opera House"
        else:
            venue_display = venue_name
        # Non-SOH venues (e.g. St Philip's Church) keep their own name
        if venue_name.lower() not in ('concert hall', 'utzon room', 'joan sutherland theatre',
                                      'drama theatre', 'playhouse', 'studio', 'sydney opera house'):
            venue_display = venue_name

        for dt in dates:
            performances.append(Performance(
                performer=presenter,
                title=title,
                date=dt,
                venue_name=venue_display,
                venue_address=lookup_venue_address(venue_display),
                url=event_url,
                source="Sydney Opera House",
                time_confirmed=time_confirmed
            ))

    print(f"  Found {len(performances)} events from Sydney Opera House")
    return performances


def scrape_willoughby_symphony(session: requests.Session) -> List[Performance]:
    """
    Scrape performances from Willoughby Symphony Orchestra website.
    This scraper fetches the events list and then visits individual event pages
    to get accurate date/time information. If no explicit date+time is found,
    the event is included date-only ("Time TBA") - times are never fabricated.
    """
    print("Scraping Willoughby Symphony Orchestra...")
    performances = []

    base_url = "https://www.willoughbysymphony.com.au"
    events_url = f"{base_url}/Events"

    response = make_request(events_url, session)
    if not response:
        print("  Could not scrape Willoughby Symphony Orchestra")
        return performances

    soup = BeautifulSoup(response.text, 'html.parser')

    # Find all links to individual event pages
    event_links = []
    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        if '/Events/' in href and href != '/Events' and href != '/Events/':
            full_url = f"{base_url}{href}" if not href.startswith('http') else href
            if full_url not in event_links:
                event_links.append(full_url)

    print(f"  Found {len(event_links)} event pages to check...")

    for event_url in event_links[:MAX_EVENT_PAGES]:
        polite_delay()

        try:
            event_response = make_request(event_url, session)
            if not event_response:
                continue

            event_soup = BeautifulSoup(event_response.text, 'html.parser')

            title_elem = event_soup.find(['h1', 'h2'])
            if not title_elem:
                continue
            title = title_elem.get_text(strip=True)
            if not title:
                continue

            page_text = event_soup.get_text(' ', strip=True)

            # Explicit "Saturday, 14 February 2026, 7:00 PM" style datetimes
            dates = extract_explicit_datetimes(page_text)

            if dates:
                for dt in dates:
                    performances.append(Performance(
                        performer="Willoughby Symphony Orchestra",
                        title=title,
                        date=dt,
                        venue_name="The Concourse Concert Hall",
                        venue_address=lookup_venue_address('the concourse'),
                        url=event_url,
                        source="Willoughby Symphony"
                    ))
            else:
                # Date-only fallback: include the event but mark the time as
                # unknown rather than inventing one.
                date_matches = DATE_ONLY_PATTERN.findall(page_text)
                for day, month, year in date_matches[:2]:
                    dt = parse_date_flexible(f"{day} {month} {year}")
                    if dt:
                        performances.append(Performance(
                            performer="Willoughby Symphony Orchestra",
                            title=title,
                            date=dt,
                            venue_name="The Concourse Concert Hall",
                            venue_address=lookup_venue_address('the concourse'),
                            url=event_url,
                            source="Willoughby Symphony",
                            time_confirmed=False
                        ))

        except Exception as e:
            print(f"  [WARNING] Error parsing WSO event page: {e}")
            continue

    print(f"  Found {len(performances)} events from Willoughby Symphony Orchestra")
    return performances


def scrape_the_concourse(session: requests.Session) -> List[Performance]:
    """
    Scrape the Classical Music genre listing at The Concourse, Chatswood.

    This is both an additional source (KPO, Sydney Mozart Society, Live at
    Lunch, visiting orchestras...) and a crosscheck for Willoughby Symphony,
    whose home venue is The Concourse. Duplicate performances found on both
    sites are merged during deduplication.
    """
    print("Scraping The Concourse (Chatswood)...")
    performances = []

    base_url = "https://www.theconcourse.com.au"
    listing_url = f"{base_url}/genre/music-classical/"

    response = make_request(listing_url, session)
    if not response:
        print("  Could not scrape The Concourse")
        return performances

    soup = BeautifulSoup(response.text, 'html.parser')

    # Event cards link to /event/<slug>/ pages
    event_links = []
    for link in soup.find_all('a', href=True):
        href = link['href']
        if '/event/' in href:
            full_url = href if href.startswith('http') else f"{base_url}{href}"
            if full_url not in event_links:
                event_links.append(full_url)

    print(f"  Found {len(event_links)} event pages to check...")

    for event_url in event_links[:MAX_EVENT_PAGES]:
        polite_delay()

        try:
            event_response = make_request(event_url, session)
            if not event_response:
                continue

            event_soup = BeautifulSoup(event_response.text, 'html.parser')

            title_elem = event_soup.find('h1')
            title = title_elem.get_text(strip=True) if title_elem else ''
            if not title:
                continue
            # Skip season-package pages: they aggregate every concert date and
            # would duplicate each performance already listed individually.
            if re.search(r'subscription|season package', title, re.IGNORECASE):
                continue
            # Normalize SHOUTING titles for readability
            if title.isupper():
                title = title.title()

            page_text = event_soup.get_text(' ', strip=True)
            dates = extract_explicit_datetimes(page_text)
            if not dates:
                continue

            # Derive the performer from an "Orchestra: Title" style prefix
            performer = "Various artists"
            if ':' in title:
                prefix = title.split(':', 1)[0]
                if re.search(r'orchestra|symphony|philharmonia|society|choir|ensemble|quartet|band',
                             prefix, re.IGNORECASE):
                    performer = prefix.strip()

            for dt in dates:
                performances.append(Performance(
                    performer=performer,
                    title=title,
                    date=dt,
                    venue_name="The Concourse Concert Hall",
                    venue_address=lookup_venue_address('the concourse'),
                    url=event_url,
                    source="The Concourse"
                ))

        except Exception as e:
            print(f"  [WARNING] Error parsing Concourse event page: {e}")
            continue

    print(f"  Found {len(performances)} events from The Concourse")
    return performances


def scrape_north_sydney_symphony(session: requests.Session) -> List[Performance]:
    """
    Scrape performances from North Sydney Symphony Orchestra website.

    The NSSO site (WebsiteBuilder) serves an empty HTML shell; the actual page
    content lives in JavaScript content files on storage.googleapis.com that
    are referenced from the shell. Those files contain the concert details as
    escaped HTML, e.g. "SATURDAY 28TH MARCH 2026, 7.30pm / The Verbrugghen
    Hall, Conservatorium of Music".
    """
    print("Scraping North Sydney Symphony Orchestra...")
    performances = []

    url = "https://www.nsso.org.au/concerts"

    response = make_request(url, session)
    if not response:
        print("  Could not scrape North Sydney Symphony Orchestra")
        return performances

    # Content files referenced by the page shell
    content_urls = re.findall(
        r'https://storage\.googleapis\.com/te-websitebuilder-sites/[^\s"\'<>\\]+\.js[^\s"\'<>\\]*',
        response.text
    )
    # Keep unique URLs, ignoring cache-buster query variants
    seen_paths = set()
    unique_urls = []
    for u in content_urls:
        path = u.split('?')[0]
        if path not in seen_paths:
            seen_paths.add(path)
            unique_urls.append(u)

    print(f"  Found {len(unique_urls)} content files to check...")

    file_texts = []
    for content_url in unique_urls[:MAX_EVENT_PAGES]:
        polite_delay()
        content_response = make_request(content_url, session)
        if not content_response:
            continue
        raw = content_response.text
        # The content is JSON-escaped HTML; unescape and strip tags
        raw = raw.replace('\\"', '"').replace('\\/', '/').replace('\\n', ' ')
        raw = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), raw)
        text = re.sub(r'<[^>]+>', ' ', raw)
        text = text.replace('&nbsp;', ' ').replace('&quot;', '"').replace('&amp;', '&')
        file_texts.append(text)
    combined_text = " ".join(file_texts)

    def map_nsso_venue(venue_hint: str) -> str:
        """
        Map free-text venue hints to a known NSSO venue name.
        Whitespace is collapsed entirely because the source files sometimes
        break words apart (e.g. "Verbrugghen Hal l").
        """
        hint = re.sub(r'[\s.]+', '', venue_hint.lower())
        for known, canonical in (
            ('verbrugghenhall', 'Verbrugghen Hall'),
            ('stleonardspark', 'St Leonards Park'),
            ('smithauditorium', 'Smith Auditorium'),
            ('northsydneygirls', 'North Sydney Girls High School'),
            ('nsghs', 'North Sydney Girls High School'),
        ):
            if known in hint:
                return canonical
        return 'See NSSO website'

    seen = set()

    def add_performance(dt: datetime, venue_hint: str):
        if not dt or dt in seen:
            return
        seen.add(dt)
        venue_name = map_nsso_venue(venue_hint)
        performances.append(Performance(
            performer="North Sydney Symphony Orchestra",
            title="North Sydney Symphony Orchestra in Concert",
            date=dt,
            venue_name=venue_name,
            venue_address=lookup_venue_address(venue_name, 'North Sydney area'),
            url=url,
            source="North Sydney Symphony"
        ))

    # 1. Season summary lists: year-less entries like
    #    "19 Sep, 7.30pm - Verbrugghen Hall". The year is taken from the
    #    "2026 CONCERT DATES" heading in the same content file, so past-season
    #    leftovers can never be mistaken for upcoming concerts.
    season_entry = re.compile(
        r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+'
        r'(\d{1,2})[.:](\d{2})\s*(am|pm)\s*[-–]\s*([A-Za-z.\'’ ]{3,40})',
        re.IGNORECASE
    )
    for text in file_texts:
        year_match = re.search(r'(\d{4})\s+CONCERT\s+DATES', text, re.IGNORECASE)
        if not year_match:
            continue
        year = year_match.group(1)
        for m in season_entry.finditer(text):
            day, month, hour, minute, ampm, venue_hint = m.groups()
            dt = parse_date_flexible(f"{day} {month} {year}", f"{hour}:{minute} {ampm.upper()}")
            add_performance(dt, venue_hint)

    # 2. Full-form headers, e.g. "SATURDAY 28TH MARCH 2026, 7.30pm" followed by venue
    full_pattern = re.compile(
        rf'(?:{DAY_NAMES})\s+(\d{{1,2}})(?:ST|ND|RD|TH)?\s+({MONTH_NAMES})\s+(\d{{4}}),?\s+'
        rf'(\d{{1,2}})[.:](\d{{2}})\s*(am|pm)\s+(?:The\s+)?([A-Za-z][A-Za-z .,\']{{3,60}})',
        re.IGNORECASE
    )
    for m in full_pattern.finditer(combined_text):
        day, month, year, hour, minute, ampm, venue_hint = m.groups()
        dt = parse_date_flexible(f"{day} {month} {year}", f"{hour}:{minute} {ampm.upper()}")
        add_performance(dt, venue_hint)

    print(f"  Found {len(performances)} events from North Sydney Symphony Orchestra")
    return performances


# =============================================================================
# DEDUPLICATION AND SORTING
# =============================================================================

def deduplicate_performances(performances: List[Performance]) -> List[Performance]:
    """
    Remove duplicate performances based on title, date, and venue.
    Keeps the first occurrence found.
    """
    seen_keys = set()
    unique_performances = []

    for perf in performances:
        key = perf.unique_key()
        if key not in seen_keys:
            seen_keys.add(key)
            unique_performances.append(perf)

    return unique_performances


def suppress_unconfirmed_duplicates(performances: List[Performance]) -> List[Performance]:
    """
    Drop "Time TBA" entries when another source supplies the same concert
    (same normalized title, same day, same venue) with a confirmed time.
    E.g. the Willoughby Symphony site often omits times that The Concourse
    lists exactly - the timed entry should win.
    """
    def day_key(perf: Performance) -> str:
        venue_norm = re.sub(r'[^a-z0-9]+', ' ', perf.venue_name.lower()).strip()
        return f"{normalize_title(perf.title)}|{perf.date.strftime('%Y-%m-%d')}|{venue_norm}"

    confirmed_days = {day_key(p) for p in performances
                      if p.time_confirmed and not (p.date.hour == 0 and p.date.minute == 0)}

    kept = []
    for perf in performances:
        is_tba = not perf.time_confirmed or (perf.date.hour == 0 and perf.date.minute == 0)
        if is_tba and day_key(perf) in confirmed_days:
            continue
        kept.append(perf)
    return kept


def sort_performances(performances: List[Performance]) -> List[Performance]:
    """
    Sort performances chronologically by date and time.
    """
    return sorted(performances, key=lambda p: p.date)


# =============================================================================
# HTML OUTPUT GENERATION
# =============================================================================

def escape_html(text: str) -> str:
    """Escape HTML entities in text content."""
    return (text.replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;').replace('"', '&quot;'))


def generate_html(performances: List[Performance],
                  source_counts: Dict[str, int],
                  output_file: str = "sydney_matinees.html"):
    """
    Generate a self-contained HTML file with all performances.
    Matinee performances are highlighted with a light yellow background.
    A per-source status line makes silent scraper failures visible.
    """

    # Count matinees for summary
    matinee_count = sum(1 for p in performances if p.is_matinee)
    total_count = len(performances)

    # Generate the current timestamp
    generated_time = datetime.now().strftime("%A, %d %B %Y at %I:%M %p")

    # Per-source status line (a zero flags a possibly-broken scraper)
    source_bits = []
    for name, count in source_counts.items():
        cls = 'source-ok' if count > 0 else 'source-fail'
        note = '' if count > 0 else ' &#9888;'
        source_bits.append(f'<span class="{cls}">{escape_html(name)}: {count}{note}</span>')
    source_status = ' &middot; '.join(source_bits)

    html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sydney Classical Music - Matinee Finder</title>
    <style>
        /* Reset and base styles */
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: Georgia, 'Times New Roman', serif;
            line-height: 1.6;
            color: #333;
            background-color: #fff;
            padding: 20px;
            max-width: 1000px;
            margin: 0 auto;
        }}

        /* Header styles */
        header {{
            text-align: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 2px solid #333;
        }}

        h1 {{
            font-size: 2em;
            margin-bottom: 10px;
            color: #1a1a1a;
        }}

        .subtitle {{
            font-style: italic;
            color: #666;
            margin-bottom: 10px;
        }}

        .summary {{
            background-color: #f5f5f5;
            padding: 15px;
            border-radius: 5px;
            margin-top: 15px;
        }}

        .summary strong {{
            color: #8B4513;
        }}

        .source-status {{
            font-size: 0.85em;
            color: #666;
            margin-top: 8px;
        }}

        .source-fail {{
            color: #b03a2e;
            font-weight: bold;
        }}

        /* Legend */
        .legend {{
            display: flex;
            justify-content: center;
            gap: 30px;
            margin: 20px 0;
            padding: 10px;
            background-color: #fafafa;
            border-radius: 5px;
        }}

        .legend-item {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .legend-color {{
            width: 20px;
            height: 20px;
            border: 1px solid #ccc;
            border-radius: 3px;
        }}

        .legend-matinee {{
            background-color: #FFFACD;
        }}

        .legend-evening {{
            background-color: #fff;
        }}

        /* Performance list */
        .performances {{
            list-style: none;
        }}

        .performance {{
            padding: 20px;
            margin-bottom: 15px;
            border: 1px solid #ddd;
            border-radius: 5px;
            transition: box-shadow 0.2s ease;
        }}

        .performance:hover {{
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .performance.matinee {{
            background-color: #FFFACD;
            border-left: 4px solid #FFD700;
        }}

        .performance.evening {{
            background-color: #fff;
            border-left: 4px solid #666;
        }}

        .performance-title {{
            font-size: 1.3em;
            margin-bottom: 8px;
        }}

        .performance-title a {{
            color: #1a5276;
            text-decoration: none;
        }}

        .performance-title a:hover {{
            text-decoration: underline;
        }}

        .performance-meta {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 10px;
            font-size: 0.95em;
            color: #555;
        }}

        .performance-meta dt {{
            font-weight: bold;
            color: #333;
        }}

        .performance-meta dd {{
            margin-bottom: 8px;
        }}

        .matinee-badge {{
            display: inline-block;
            background-color: #FFD700;
            color: #333;
            padding: 2px 8px;
            border-radius: 3px;
            font-size: 0.8em;
            font-weight: bold;
            margin-left: 10px;
        }}

        .tba-badge {{
            display: inline-block;
            background-color: #ddd;
            color: #555;
            padding: 2px 8px;
            border-radius: 3px;
            font-size: 0.8em;
            font-weight: bold;
            margin-left: 10px;
        }}

        .source-tag {{
            display: inline-block;
            background-color: #e8e8e8;
            color: #666;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.75em;
            margin-left: 8px;
            vertical-align: middle;
        }}

        /* Footer */
        footer {{
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
            text-align: center;
            font-size: 0.9em;
            color: #666;
        }}

        /* No results message */
        .no-results {{
            text-align: center;
            padding: 40px;
            color: #666;
            font-style: italic;
        }}

        /* Responsive adjustments */
        @media (max-width: 600px) {{
            body {{
                padding: 10px;
            }}

            h1 {{
                font-size: 1.5em;
            }}

            .legend {{
                flex-direction: column;
                gap: 10px;
            }}

            .performance-meta {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>
<body>
    <header>
        <h1>Sydney Classical Music</h1>
        <p class="subtitle">Matinee Performance Finder</p>
        <div class="summary">
            <p>Found <strong>{total_count}</strong> upcoming performances</p>
            <p><strong>{matinee_count}</strong> matinee shows (before 5:00 PM) highlighted in yellow</p>
            <p class="source-status">{source_status}</p>
        </div>
    </header>

    <div class="legend">
        <div class="legend-item">
            <div class="legend-color legend-matinee"></div>
            <span>Matinee (before 5:00 PM)</span>
        </div>
        <div class="legend-item">
            <div class="legend-color legend-evening"></div>
            <span>Evening or time TBA</span>
        </div>
    </div>

    <main>
'''

    if performances:
        html_content += '        <ul class="performances">\n'

        for perf in performances:
            matinee_class = "matinee" if perf.is_matinee else "evening"
            badge = ''
            if perf.is_matinee:
                badge = '<span class="matinee-badge">MATINEE</span>'
            elif perf.time_str == "Time TBA":
                badge = '<span class="tba-badge">TIME TBA</span>'

            title_escaped = escape_html(perf.title)
            performer_escaped = escape_html(perf.performer)
            venue_escaped = escape_html(perf.venue_name)
            address_escaped = escape_html(perf.venue_address)
            source_escaped = escape_html(perf.source)
            url_escaped = escape_html(perf.url)

            html_content += f'''            <li class="performance {matinee_class}">
                <h2 class="performance-title">
                    <a href="{url_escaped}" target="_blank" rel="noopener">{title_escaped}</a>
                    {badge}
                    <span class="source-tag">{source_escaped}</span>
                </h2>
                <dl class="performance-meta">
                    <div>
                        <dt>Performer</dt>
                        <dd>{performer_escaped}</dd>
                    </div>
                    <div>
                        <dt>Date</dt>
                        <dd>{perf.date_str}</dd>
                    </div>
                    <div>
                        <dt>Time</dt>
                        <dd>{perf.time_str}</dd>
                    </div>
                    <div>
                        <dt>Venue</dt>
                        <dd>{venue_escaped}<br><small>{address_escaped}</small></dd>
                    </div>
                </dl>
            </li>
'''

        html_content += '        </ul>\n'
    else:
        html_content += '''        <div class="no-results">
            <p>No performances found. This could be because:</p>
            <ul style="list-style: disc; margin: 20px auto; max-width: 400px; text-align: left;">
                <li>The websites have changed their structure</li>
                <li>There are no upcoming performances listed</li>
                <li>The websites were temporarily unavailable</li>
            </ul>
            <p>Try running the script again later.</p>
        </div>
'''

    html_content += f'''    </main>

    <footer>
        <p>Generated on {generated_time}</p>
        <p>Data sources: Sydney Symphony Orchestra, Sydney Opera House,<br>
           Willoughby Symphony Orchestra, The Concourse, North Sydney Symphony Orchestra</p>
        <p><em>Sydney Matinee Finder v2.0</em></p>
    </footer>
</body>
</html>
'''

    # Write the HTML file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_content)

    print(f"\nHTML file generated: {output_file}")


# =============================================================================
# MAIN EXECUTION
# =============================================================================

def main():
    """
    Main function that orchestrates the scraping, processing, and output generation.
    """
    print("=" * 60)
    print("Sydney Matinee Music Finder v2.0")
    print("=" * 60)
    if SYDNEY_TZ is None:
        print("[NOTE] zoneinfo/tzdata unavailable - using built-in DST rule.")
        print("       For guaranteed accuracy run: pip install tzdata")
    print()

    # Create a session for connection pooling and cookie handling
    session = requests.Session()

    # Scrape each source with polite delays between requests
    scrapers = [
        ("Sydney Symphony Orchestra", scrape_sydney_symphony_api),
        ("Sydney Opera House", scrape_sydney_opera_house),
        ("Willoughby Symphony", scrape_willoughby_symphony),
        ("The Concourse", scrape_the_concourse),
        ("North Sydney Symphony", scrape_north_sydney_symphony),
    ]

    all_performances = []
    source_counts = {}
    for name, scraper in scrapers:
        performances = scraper(session)
        source_counts[name] = len(performances)
        all_performances.extend(performances)
        polite_delay()

    print()
    print(f"Total events collected: {len(all_performances)}")

    # Remove duplicates (also merges Willoughby <-> Concourse crosscheck overlap)
    print("Removing duplicates...")
    unique_performances = deduplicate_performances(all_performances)
    unique_performances = suppress_unconfirmed_duplicates(unique_performances)
    print(f"Unique events after deduplication: {len(unique_performances)}")

    # Sort chronologically
    print("Sorting by date...")
    sorted_performances = sort_performances(unique_performances)

    # Filter to only future performances
    now = datetime.now()
    future_performances = [p for p in sorted_performances if p.date >= now]
    print(f"Future events: {len(future_performances)}")

    # Count matinees
    matinee_count = sum(1 for p in future_performances if p.is_matinee)
    tba_count = sum(1 for p in future_performances if p.time_str == "Time TBA")
    evening_count = len(future_performances) - matinee_count - tba_count
    print(f"Matinee performances: {matinee_count}")
    print(f"Evening performances: {evening_count}")
    print(f"Time TBA (unconfirmed): {tba_count}")

    # Generate HTML output
    print()
    print("Generating HTML file...")
    generate_html(future_performances, source_counts)

    print()
    print("=" * 60)
    print("Done! Open 'sydney_matinees.html' in your web browser.")
    print("=" * 60)


if __name__ == "__main__":
    main()
