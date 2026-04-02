#!/usr/bin/env python3
"""
Sydney Matinee Music Finder v1.0

This script scrapes upcoming classical and community orchestra performances in Sydney,
with a special focus on matinee shows (performances starting before 5:00 PM).

Data Sources:
- Sydney Opera House (What's On - Classical Music)
- Sydney Symphony Orchestra (API endpoint)
- Willoughby Symphony Orchestra (Events page + individual event pages)
- North Sydney Symphony Orchestra (Concerts page)

Output: A self-contained HTML file (sydney_matinees.html) that can be opened in any browser.

Security Note: This script only connects to the four target websites listed above.
It performs read-only operations and writes only the output HTML file.
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import time
import re
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import json

# =============================================================================
# CONFIGURATION
# =============================================================================

# Custom User-Agent to identify ourselves politely
USER_AGENT = "SydneyMatineeFinder/1.0 (Bot for personal, non-commercial use)"

# Delay between requests to be polite to servers (in seconds)
REQUEST_DELAY = 2.5

# Matinee cutoff time (performances before this time are considered matinees)
MATINEE_CUTOFF_HOUR = 17  # 5:00 PM

# Request timeout (seconds)
REQUEST_TIMEOUT = 30

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

# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class Performance:
    """Represents a single performance/concert."""
    performer: str           # Orchestra or performer name
    title: str               # Concert title
    date: datetime           # Date and time of performance
    venue_name: str          # Name of the venue
    venue_address: str       # Address of the venue
    url: str                 # Direct link to booking/info page
    source: str              # Which website this came from

    @property
    def is_matinee(self) -> bool:
        """Check if this performance is a matinee (before 5 PM)."""
        return self.date.hour < MATINEE_CUTOFF_HOUR

    @property
    def time_str(self) -> str:
        """Return formatted time string."""
        if self.date.hour == 0 and self.date.minute == 0:
            return "Time TBA"
        return self.date.strftime("%I:%M %p").lstrip("0")

    @property
    def date_str(self) -> str:
        """Return formatted date string."""
        return self.date.strftime("%A, %d %B %Y")

    def unique_key(self) -> str:
        """Generate a unique key for deduplication."""
        # Normalize for comparison: lowercase, remove extra spaces
        title_norm = re.sub(r'\s+', ' ', self.title.lower().strip())
        date_norm = self.date.strftime("%Y-%m-%d %H:%M")
        venue_norm = self.venue_name.lower().strip()
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


def parse_iso_datetime(iso_str: str, convert_utc_to_sydney: bool = True) -> Optional[datetime]:
    """
    Parse an ISO 8601 datetime string.
    Handles formats like: 2026-02-25T19:30:00.000+11:00 or 2026-01-30T07:00:00.000Z

    If the time is in UTC (ends with Z), convert to Sydney time by adding 11 hours
    (AEDT - Australian Eastern Daylight Time, used Oct-Apr).

    Note: This is a simplified timezone handling that doesn't account for
    daylight saving transitions. For precise handling, use pytz library.
    """
    if not iso_str:
        return None

    is_utc = iso_str.endswith('Z')

    try:
        # Remove milliseconds and timezone for simpler parsing
        # Format: 2026-02-25T19:30:00.000+11:00 or 2026-01-30T07:00:00.000Z
        clean_str = re.sub(r'\.\d{3}', '', iso_str)  # Remove .000
        clean_str = clean_str.rstrip('Z')  # Remove Z suffix
        clean_str = re.sub(r'[+-]\d{2}:\d{2}$', '', clean_str)  # Remove timezone offset

        parsed_dt = datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")

        # Convert UTC to Sydney time if needed
        # Sydney is UTC+11 during daylight saving (Oct-Apr) and UTC+10 otherwise
        if is_utc and convert_utc_to_sydney:
            # Simplified: assume AEDT (UTC+11) for most of the concert season
            # This covers Oct-Apr which is when most concerts occur
            month = parsed_dt.month
            if month >= 4 and month <= 10:
                # AEST: UTC+10
                parsed_dt = parsed_dt + timedelta(hours=10)
            else:
                # AEDT: UTC+11
                parsed_dt = parsed_dt + timedelta(hours=11)

        return parsed_dt

    except ValueError:
        try:
            # Try simpler format
            return datetime.strptime(iso_str[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None


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
        "%I.%M %p",      # 2.00 PM
        "%I.%M%p",       # 2.00PM
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

    API Structure:
    {
      "docs": [
        {
          "title": "Concert Name",
          "slug": "concert-slug",
          "startDate": "2026-01-30T07:00:00.000Z",
          "venue": {"title": "Venue Name", "slug": "venue-slug"},
          "eventInstances": {
            "docs": [
              {"startDate": "2026-01-30T07:00:00.000Z"},
              {"startDate": "2026-01-31T07:00:00.000Z"}
            ]
          }
        }
      ]
    }
    """
    print("Scraping Sydney Symphony Orchestra (API)...")
    performances = []

    # The SSO has a public API endpoint that returns event data
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

                # Map common venues to addresses
                venue_addresses = {
                    'concert hall': 'Bennelong Point, Sydney NSW 2000',
                    'utzon room': 'Bennelong Point, Sydney NSW 2000',
                    'sydney opera house': 'Bennelong Point, Sydney NSW 2000',
                    'sydney town hall': '483 George St, Sydney NSW 2000',
                    'city recital hall': '2 Angel Pl, Sydney NSW 2000',
                    "st james' church": "173 King St, Sydney NSW 2000",
                    "st james'": "173 King St, Sydney NSW 2000",
                    'state theatre': '49 Market St, Sydney NSW 2000',
                }
                venue_address = 'Sydney, NSW'
                for key, addr in venue_addresses.items():
                    if key in venue_name.lower():
                        venue_address = addr
                        break

                # Get all event instances (individual performance dates/times)
                # The API returns eventInstances as {"docs": [...]}
                instances_data = event.get('eventInstances', {})
                if isinstance(instances_data, dict):
                    instances = instances_data.get('docs', [])
                elif isinstance(instances_data, list):
                    instances = instances_data
                else:
                    instances = []

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

                # If no instances found, try using startDate from the event itself
                if not instances:
                    start_date_str = event.get('startDate', '')
                    if start_date_str:
                        perf_date = parse_iso_datetime(start_date_str)
                        if perf_date:
                            instance_key = f"{title}|{perf_date.strftime('%Y-%m-%d %H:%M')}"
                            if instance_key not in seen_instances:
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
    Scrape performances from Sydney Opera House website.
    Target: Classical music and concerts.
    Note: SOH uses heavy JavaScript rendering, so results may be limited.
    """
    print("Scraping Sydney Opera House...")
    performances = []

    # The SOH uses dynamic rendering, but we'll try to get what we can
    url = "https://www.sydneyoperahouse.com/whats-on?genre%5B%5D=1436&date_range%5Bmin%5D=2026-01-23&date_range%5Bmax%5D=2027-01-23"

    response = make_request(url, session)
    if not response:
        print("  Could not scrape Sydney Opera House")
        return performances

    soup = BeautifulSoup(response.text, 'html.parser')

    # Look for JSON data embedded in the page (common pattern for React/Next.js sites)
    scripts = soup.find_all('script', type='application/json')
    for script in scripts:
        try:
            data = json.loads(script.string)
            # Try to find event data in the JSON
            # This would need to be adapted based on actual page structure
        except:
            continue

    # Try to find event cards in the HTML
    # SOH typically uses structured data for events
    event_links = soup.find_all('a', href=lambda x: x and '/whats-on/' in x and x.count('/') > 2)

    seen_urls = set()
    for link in event_links[:50]:
        try:
            href = link.get('href', '')
            if href in seen_urls or not href:
                continue
            seen_urls.add(href)

            full_url = f"https://www.sydneyoperahouse.com{href}" if not href.startswith('http') else href

            # Try to get title from link text or nearby elements
            title = link.get_text(strip=True)
            if not title or len(title) < 3:
                title_elem = link.find(['h2', 'h3', 'h4', 'span'])
                title = title_elem.get_text(strip=True) if title_elem else ""

            if not title or len(title) < 3:
                continue

            # Skip navigation links
            if title.lower() in ['view', 'book', 'more', 'details', 'buy tickets']:
                continue

            # Look for date information in parent container
            parent = link.find_parent(['article', 'div', 'li'])
            date_text = ""
            if parent:
                date_elem = parent.find(['time', 'span'], class_=lambda x: x and 'date' in str(x).lower())
                if date_elem:
                    date_text = date_elem.get('datetime', '') or date_elem.get_text(strip=True)

            perf_date = parse_iso_datetime(date_text) or parse_date_flexible(date_text)
            if not perf_date:
                # For SOH, we might not get dates from the listing page
                # The event page itself would have the details
                continue

            performances.append(Performance(
                performer="Sydney Opera House",
                title=title,
                date=perf_date,
                venue_name="Sydney Opera House",
                venue_address="Bennelong Point, Sydney NSW 2000",
                url=full_url,
                source="Sydney Opera House"
            ))

        except Exception as e:
            print(f"  [WARNING] Error parsing SOH event: {e}")
            continue

    print(f"  Found {len(performances)} events from Sydney Opera House")
    return performances


def scrape_willoughby_symphony(session: requests.Session) -> List[Performance]:
    """
    Scrape performances from Willoughby Symphony Orchestra website.
    This scraper fetches the events list and then visits individual event pages
    to get accurate date/time information.
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
        # Look for event page links (typically /Events/EventName format)
        if '/Events/' in href and href != '/Events' and href != '/Events/':
            full_url = f"{base_url}{href}" if not href.startswith('http') else href
            if full_url not in event_links:
                event_links.append(full_url)

    print(f"  Found {len(event_links)} event pages to check...")

    # Visit each event page to get detailed information
    for event_url in event_links[:15]:  # Limit to prevent too many requests
        polite_delay()

        try:
            event_response = make_request(event_url, session)
            if not event_response:
                continue

            event_soup = BeautifulSoup(event_response.text, 'html.parser')

            # Extract title from the page
            title_elem = event_soup.find(['h1', 'h2'])
            if not title_elem:
                continue
            title = title_elem.get_text(strip=True)
            if not title or 'willoughby' not in title.lower():
                # Try to make title more descriptive
                pass

            # Get all text content to search for dates and times
            page_text = event_soup.get_text()

            # Find all date/time patterns
            # Pattern: "Saturday, 14 February 2026, 7:00 PM"
            datetime_pattern = r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4}),?\s+(\d{1,2})[:\.](\d{2})\s*(AM|PM|am|pm)'

            matches = re.findall(datetime_pattern, page_text, re.IGNORECASE)

            for match in matches:
                day_name, day, month, year, hour, minute, ampm = match
                date_str = f"{day} {month} {year}"
                time_str = f"{hour}:{minute} {ampm.upper()}"

                perf_date = parse_date_flexible(date_str, time_str)
                if perf_date:
                    performances.append(Performance(
                        performer="Willoughby Symphony Orchestra",
                        title=title,
                        date=perf_date,
                        venue_name="The Concourse Concert Hall",
                        venue_address="409 Victoria Ave, Chatswood NSW 2067",
                        url=event_url,
                        source="Willoughby Symphony"
                    ))

            # If no datetime pattern found, try simpler date patterns
            if not matches:
                date_pattern = r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})'
                date_matches = re.findall(date_pattern, page_text, re.IGNORECASE)

                time_pattern = r'(\d{1,2})[:\.](\d{2})\s*(AM|PM|am|pm)'
                time_matches = re.findall(time_pattern, page_text, re.IGNORECASE)

                if date_matches:
                    for date_match in date_matches[:2]:  # Limit to first 2 dates
                        day, month, year = date_match
                        date_str = f"{day} {month} {year}"

                        # Use first time found, or default to 7:30 PM (common concert time)
                        if time_matches:
                            hour, minute, ampm = time_matches[0]
                            time_str = f"{hour}:{minute} {ampm.upper()}"
                        else:
                            time_str = "7:30 PM"

                        perf_date = parse_date_flexible(date_str, time_str)
                        if perf_date:
                            performances.append(Performance(
                                performer="Willoughby Symphony Orchestra",
                                title=title,
                                date=perf_date,
                                venue_name="The Concourse Concert Hall",
                                venue_address="409 Victoria Ave, Chatswood NSW 2067",
                                url=event_url,
                                source="Willoughby Symphony"
                            ))

        except Exception as e:
            print(f"  [WARNING] Error parsing WSO event page: {e}")
            continue

    print(f"  Found {len(performances)} events from Willoughby Symphony Orchestra")
    return performances


def scrape_north_sydney_symphony(session: requests.Session) -> List[Performance]:
    """
    Scrape performances from North Sydney Symphony Orchestra website.
    """
    print("Scraping North Sydney Symphony Orchestra...")
    performances = []

    url = "https://www.nsso.org.au/concerts"

    response = make_request(url, session)
    if not response:
        print("  Could not scrape North Sydney Symphony Orchestra")
        return performances

    soup = BeautifulSoup(response.text, 'html.parser')

    # Get all text content
    page_text = soup.get_text()

    # NSSO typically lists concerts with dates
    # Look for patterns like "28 March 2026" or "Saturday 28 March 2026"

    # Find all headings that might be concert titles
    headings = soup.find_all(['h1', 'h2', 'h3', 'h4'])

    seen_dates = set()

    for heading in headings:
        title = heading.get_text(strip=True)
        if not title or len(title) < 5:
            continue

        # Skip navigation/generic headings
        skip_words = ['concert', 'ticket', 'about', 'contact', 'home', 'menu', 'nsso', 'north sydney']
        if title.lower() in skip_words:
            continue

        # Look for date in nearby content
        parent = heading.find_parent(['section', 'article', 'div'])
        if parent:
            parent_text = parent.get_text()

            # Look for date pattern
            date_pattern = r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})'
            date_match = re.search(date_pattern, parent_text, re.IGNORECASE)

            if date_match:
                day, month, year = date_match.groups()
                date_str = f"{day} {month} {year}"

                # Avoid duplicate dates
                if date_str in seen_dates:
                    continue
                seen_dates.add(date_str)

                # Look for time
                time_pattern = r'(\d{1,2})[:\.](\d{2})\s*(AM|PM|am|pm)'
                time_match = re.search(time_pattern, parent_text, re.IGNORECASE)

                if time_match:
                    hour, minute, ampm = time_match.groups()
                    time_str = f"{hour}:{minute} {ampm.upper()}"
                else:
                    time_str = "2:30 PM"  # NSSO often has afternoon concerts

                perf_date = parse_date_flexible(date_str, time_str)
                if perf_date:
                    # Try to find a link
                    link = parent.find('a', href=True)
                    event_url = url
                    if link:
                        href = link.get('href', '')
                        if href.startswith('/'):
                            event_url = f"https://www.nsso.org.au{href}"
                        elif href.startswith('http'):
                            event_url = href

                    performances.append(Performance(
                        performer="North Sydney Symphony Orchestra",
                        title=title if len(title) > 10 else f"NSSO Concert - {date_str}",
                        date=perf_date,
                        venue_name="Various venues",
                        venue_address="North Sydney area",
                        url=event_url,
                        source="North Sydney Symphony"
                    ))

    # Also try to find concerts from page structure
    # Look for links that might be to concert pages
    concert_links = soup.find_all('a', href=lambda x: x and ('concert' in str(x).lower() or '202' in str(x)))

    for link in concert_links[:10]:
        try:
            href = link.get('href', '')
            if not href or href == url:
                continue

            link_text = link.get_text(strip=True)

            # Try to extract date from link text or href
            date_pattern = r'(\d{1,2})[-\s]*(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-\s]*(\d{4})'
            date_match = re.search(date_pattern, link_text + ' ' + href, re.IGNORECASE)

            if date_match:
                day, month, year = date_match.groups()
                date_str = f"{day} {month} {year}"

                if date_str in seen_dates:
                    continue
                seen_dates.add(date_str)

                perf_date = parse_date_flexible(date_str, "2:30 PM")
                if perf_date:
                    full_url = f"https://www.nsso.org.au{href}" if href.startswith('/') else href

                    performances.append(Performance(
                        performer="North Sydney Symphony Orchestra",
                        title=link_text if len(link_text) > 5 else f"NSSO Concert",
                        date=perf_date,
                        venue_name="Various venues",
                        venue_address="North Sydney area",
                        url=full_url if full_url.startswith('http') else url,
                        source="North Sydney Symphony"
                    ))

        except Exception as e:
            print(f"  [WARNING] Error parsing NSSO link: {e}")
            continue

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


def sort_performances(performances: List[Performance]) -> List[Performance]:
    """
    Sort performances chronologically by date and time.
    """
    return sorted(performances, key=lambda p: p.date)


# =============================================================================
# HTML OUTPUT GENERATION
# =============================================================================

def generate_html(performances: List[Performance], output_file: str = "sydney_matinees.html"):
    """
    Generate a self-contained HTML file with all performances.
    Matinee performances are highlighted with a light yellow background.
    """

    # Count matinees for summary
    matinee_count = sum(1 for p in performances if p.is_matinee)
    total_count = len(performances)

    # Generate the current timestamp
    generated_time = datetime.now().strftime("%A, %d %B %Y at %I:%M %p")

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

        .source-tag {{
            display: inline-block;
            background-color: #e8e8e8;
            color: #666;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.75em;
            margin-left: 8px;
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
        </div>
    </header>

    <div class="legend">
        <div class="legend-item">
            <div class="legend-color legend-matinee"></div>
            <span>Matinee (before 5:00 PM)</span>
        </div>
        <div class="legend-item">
            <div class="legend-color legend-evening"></div>
            <span>Evening performance</span>
        </div>
    </div>

    <main>
'''

    if performances:
        html_content += '        <ul class="performances">\n'

        for perf in performances:
            matinee_class = "matinee" if perf.is_matinee else "evening"
            matinee_badge = '<span class="matinee-badge">MATINEE</span>' if perf.is_matinee else ''

            # Escape HTML entities in text content
            title_escaped = perf.title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            performer_escaped = perf.performer.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            venue_escaped = perf.venue_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            address_escaped = perf.venue_address.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            source_escaped = perf.source.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

            html_content += f'''            <li class="performance {matinee_class}">
                <h2 class="performance-title">
                    <a href="{perf.url}" target="_blank" rel="noopener">{title_escaped}</a>
                    {matinee_badge}
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
        <p>Data sources: Sydney Opera House, Sydney Symphony Orchestra,<br>
           Willoughby Symphony Orchestra, North Sydney Symphony Orchestra</p>
        <p><em>Sydney Matinee Finder v1.0</em></p>
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
    print("Sydney Matinee Music Finder v1.0")
    print("=" * 60)
    print()

    # Create a session for connection pooling and cookie handling
    session = requests.Session()

    # Collect all performances from each source
    all_performances = []

    # Scrape each source with polite delays between requests

    # 1. Sydney Symphony Orchestra (using API - most reliable)
    performances = scrape_sydney_symphony_api(session)
    all_performances.extend(performances)
    polite_delay()

    # 2. Sydney Opera House
    performances = scrape_sydney_opera_house(session)
    all_performances.extend(performances)
    polite_delay()

    # 3. Willoughby Symphony Orchestra
    performances = scrape_willoughby_symphony(session)
    all_performances.extend(performances)
    polite_delay()

    # 4. North Sydney Symphony Orchestra
    performances = scrape_north_sydney_symphony(session)
    all_performances.extend(performances)

    print()
    print(f"Total events collected: {len(all_performances)}")

    # Remove duplicates
    print("Removing duplicates...")
    unique_performances = deduplicate_performances(all_performances)
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
    evening_count = len(future_performances) - matinee_count
    print(f"Matinee performances: {matinee_count}")
    print(f"Evening performances: {evening_count}")

    # Generate HTML output
    print()
    print("Generating HTML file...")
    generate_html(future_performances)

    print()
    print("=" * 60)
    print("Done! Open 'sydney_matinees.html' in your web browser.")
    print("=" * 60)


if __name__ == "__main__":
    main()
