#!/usr/bin/env python3
"""
Core data model, date handling, classification and output writers for the
Sydney Matinee Finder.

Nothing in here touches the network - see sources.py for that.

The two things most worth understanding before editing:

1. Timezones. Australia switches to daylight saving on the first Sunday of
   October and back on the first Sunday of April. A month-based
   approximation is wrong for roughly four weeks of every year, which
   showed every late-October concert an hour early and mislabelled 5pm
   shows as matinees. to_sydney_time() uses the IANA database when it can
   and falls back to the real first-Sunday rule when it cannot. Never
   reintroduce a naive datetime.now().

2. Two independent classification axes. "access" is who may book (public,
   schools, participants); "formats" is what kind of event it is (concert,
   film, masterclass...). A 10am schools concert genuinely is a matinee -
   it just isn't bookable - so is_matinee must never consult access.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import hashlib
import json
import re

try:
    from zoneinfo import ZoneInfo
    SYDNEY_TZ = ZoneInfo("Australia/Sydney")
except Exception:
    # zoneinfo lookup fails on Windows when the tzdata package is missing.
    # A manual first-Sunday DST fallback is used in to_sydney_time() below.
    SYDNEY_TZ = None

SCHEMA_VERSION = 1

# Performances starting before this hour are matinees.
MATINEE_CUTOFF_HOUR = 17

MONTH_NAMES = ("January|February|March|April|May|June|July|August|September|"
               "October|November|December")
DAY_NAMES = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"

# "Saturday, 08 August 2026 07:00 PM". Requiring the date before the time
# stops this matching on-sale notices like "9am, Wednesday 26 November 2025".
EXPLICIT_DATETIME_PATTERN = re.compile(
    rf'(?:{DAY_NAMES}),?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_NAMES})\s+(\d{{4}}),?\s+'
    rf'(\d{{1,2}})(?:[:.](\d{{2}}))?\s*(AM|PM)',
    re.IGNORECASE
)

# "21 March 2026"
DATE_ONLY_PATTERN = re.compile(
    rf'(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_NAMES})\s+(\d{{4}})',
    re.IGNORECASE
)

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


# =============================================================================
# TIME
# =============================================================================

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


def sydney_to_utc(naive_sydney: datetime) -> datetime:
    """
    Inverse of to_sydney_time: naive Sydney wall-clock -> aware UTC.
    Used for calendar feeds, which emit UTC so no client has to resolve a
    TZID reference.
    """
    if SYDNEY_TZ is not None:
        return naive_sydney.replace(tzinfo=SYDNEY_TZ).astimezone(timezone.utc)

    m, y, d = naive_sydney.month, naive_sydney.year, naive_sydney.day
    in_dst = (
        m > 10 or m < 4
        or (m == 10 and d >= _first_sunday(y, 10))
        or (m == 4 and d < _first_sunday(y, 4))
    )
    offset = 11 if in_dst else 10
    return (naive_sydney - timedelta(hours=offset)).replace(tzinfo=timezone.utc)


def now_sydney() -> datetime:
    """
    Current Sydney wall-clock time, as a naive datetime.

    Never use datetime.now() for anything date-related: GitHub Actions
    runners are UTC, and a run at 18:45 UTC is already 05:45 the next day
    in Sydney. That would shift the "future events" cutoff by a full day.
    """
    return to_sydney_time(datetime.now(timezone.utc))


def today_start_sydney() -> datetime:
    """Midnight at the start of today, Sydney time."""
    return now_sydney().replace(hour=0, minute=0, second=0, microsecond=0)


# =============================================================================
# PARSING
# =============================================================================

def parse_iso_datetime(iso_str: str, assume_utc: bool = False) -> Optional[datetime]:
    """
    Parse an ISO 8601 datetime string and return naive Sydney local time.

    - Strings with an explicit offset (or Z) are converted to Sydney time.
    - Naive strings are returned as-is, unless assume_utc=True, in which
      case they are treated as UTC first. The Sydney Opera House embeds UTC
      times in its JSON-LD without any timezone marker.
    """
    if not iso_str:
        return None

    s = iso_str.strip().replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
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
    """Parse a date string (and optional time) across many observed formats."""
    if not date_str:
        return None

    date_str = date_str.strip()
    time_str = time_str.strip() if time_str else ""

    date_formats = [
        "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%d/%m/%Y", "%Y-%m-%d",
        "%A %d %B %Y", "%A, %d %B %Y", "%a %d %b %Y", "%d %B", "%d %b",
    ]
    time_formats = ["%I:%M %p", "%I:%M%p", "%H:%M", "%I %p", "%I%p"]

    parsed_date = None
    for fmt in date_formats:
        try:
            parsed_date = datetime.strptime(date_str, fmt)
            if parsed_date.year == 1900:
                today = now_sydney()
                parsed_date = parsed_date.replace(year=today.year)
                if parsed_date < today:
                    parsed_date = parsed_date.replace(year=today.year + 1)
            break
        except ValueError:
            continue

    if not parsed_date:
        return None

    if time_str:
        time_clean = re.sub(r'\s+', ' ', time_str.upper().strip()).replace('.', ':')
        for fmt in time_formats:
            try:
                parsed_time = datetime.strptime(time_clean, fmt)
                parsed_date = parsed_date.replace(hour=parsed_time.hour,
                                                  minute=parsed_time.minute)
                break
            except ValueError:
                continue

    return parsed_date


def extract_explicit_datetimes(text: str) -> List[datetime]:
    """
    Find all "Saturday, 08 August 2026 07:00 PM" style date+times in a blob
    of text. Deduplicated, order preserved.
    """
    results, seen = [], set()
    for m in EXPLICIT_DATETIME_PATTERN.finditer(text):
        day, month, year, hour, minute, ampm = m.groups()
        minute = minute or "00"
        dt = parse_date_flexible(f"{day} {month} {year}", f"{hour}:{minute} {ampm.upper()}")
        if dt and dt not in seen:
            seen.add(dt)
            results.append(dt)
    return results


def normalize_title(title: str) -> str:
    """
    Normalize a concert title for cross-source matching: lowercase,
    alphanumerics only, standalone years removed ("Last Night of the Proms
    2026" and "Last Night Of The Proms" must compare equal).
    """
    norm = re.sub(r'[^a-z0-9]+', ' ', title.lower())
    norm = re.sub(r'\b(?:19|20)\d{2}\b', '', norm)
    return re.sub(r'\s+', ' ', norm).strip()


def lookup_venue_address(venue_name: str, default: str = 'Sydney, NSW') -> str:
    """Map a venue name to a street address if we know it."""
    lowered = venue_name.lower()
    for key, addr in VENUE_ADDRESSES.items():
        if key in lowered:
            return addr
    return default


# =============================================================================
# DATA MODEL
# =============================================================================

@dataclass
class Performance:
    """A single performance at a single date and time."""
    performer: str
    title: str
    date: datetime            # naive, Sydney local
    venue_name: str
    venue_address: str
    url: str
    source: str
    time_confirmed: bool = True

    # Populated by classify()
    display_title: str = ""
    performer_group: str = ""
    programme: Optional[str] = None
    venue_group: str = ""
    access: str = "public"
    formats: List[str] = field(default_factory=list)
    region: str = "sydney"
    needs_review: bool = False
    classified_by: str = "default"
    stale: bool = False
    ordinal: int = 0          # nth performance of this concert on this day

    @property
    def is_matinee(self) -> bool:
        """
        Before 5pm. Deliberately independent of `access`: a 10am schools
        concert is still a matinee, it just isn't bookable by the public.
        Events with no confirmed time are never matinees - we don't guess.
        """
        if not self.time_confirmed:
            return False
        if self.date.hour == 0 and self.date.minute == 0:
            return False
        return self.date.hour < MATINEE_CUTOFF_HOUR

    @property
    def time_known(self) -> bool:
        return self.time_confirmed and not (self.date.hour == 0 and self.date.minute == 0)

    @property
    def time_str(self) -> str:
        if not self.time_known:
            return "Time TBA"
        return self.date.strftime("%I:%M %p").lstrip("0")

    @property
    def date_str(self) -> str:
        return self.date.strftime("%A, %d %B %Y")

    @property
    def sort_key(self) -> int:
        """
        Sortable integer. Unknown times sort to the end of their own day,
        so a TBA event never jumps ahead of a scheduled morning concert.
        """
        day = int(self.date.strftime("%Y%m%d"))
        minute = self.date.hour * 100 + self.date.minute if self.time_known else 9999
        return day * 10000 + minute

    def event_id(self) -> str:
        """
        Stable identifier for calendar UIDs.

        The clock time is excluded so that correcting 2:00pm to 2:30pm
        updates a subscriber's existing entry instead of leaving an orphan
        beside a new one. But a matinee and an evening show of the same
        concert on the same day are genuinely different performances, and
        sharing a UID would make calendar clients silently keep only one -
        losing exactly the matinee this site exists to surface. `ordinal`
        (assigned by assign_ordinals) separates them while staying stable
        under a time change.
        """
        venue_norm = re.sub(r'[^a-z0-9]+', ' ', self.venue_name.lower()).strip()
        raw = (f"{normalize_title(self.title)}|{self.date.strftime('%Y-%m-%d')}"
               f"|{venue_norm}|{self.ordinal}")
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]

    def unique_key(self) -> str:
        """
        Deduplication key. Uses the RAW title on purpose - stripping the
        "For Schools: " prefix here would collide a 10am schools show with
        an 8pm public performance of the same programme and silently drop
        one of them.
        """
        venue_norm = re.sub(r'[^a-z0-9]+', ' ', self.venue_name.lower()).strip()
        return f"{normalize_title(self.title)}|{self.date.strftime('%Y-%m-%d %H:%M')}|{venue_norm}"


# =============================================================================
# DEDUPLICATION
# =============================================================================

def deduplicate_performances(performances: List[Performance]) -> List[Performance]:
    """Remove exact duplicates, keeping the first occurrence."""
    seen, unique = set(), []
    for perf in performances:
        key = perf.unique_key()
        if key not in seen:
            seen.add(key)
            unique.append(perf)
    return unique


def suppress_unconfirmed_duplicates(performances: List[Performance]) -> List[Performance]:
    """
    Drop "Time TBA" entries when another source has the same concert on the
    same day with a confirmed time. The Willoughby Symphony site often omits
    times that The Concourse publishes exactly; the timed entry should win.
    """
    def day_key(perf: Performance) -> str:
        venue_norm = re.sub(r'[^a-z0-9]+', ' ', perf.venue_name.lower()).strip()
        return f"{normalize_title(perf.title)}|{perf.date.strftime('%Y-%m-%d')}|{venue_norm}"

    confirmed = {day_key(p) for p in performances if p.time_known}
    return [p for p in performances if p.time_known or day_key(p) not in confirmed]


def assign_ordinals(performances: List[Performance]) -> List[Performance]:
    """
    Number the performances of the same concert on the same day, earliest
    first, so each gets a distinct but stable calendar UID. Must run before
    classify(), which resolves manual overrides by event id.
    """
    buckets: Dict[str, List[Performance]] = {}
    for perf in performances:
        venue_norm = re.sub(r'[^a-z0-9]+', ' ', perf.venue_name.lower()).strip()
        key = f"{normalize_title(perf.title)}|{perf.date.strftime('%Y-%m-%d')}|{venue_norm}"
        buckets.setdefault(key, []).append(perf)

    for group in buckets.values():
        for index, perf in enumerate(sorted(group, key=lambda p: p.sort_key)):
            perf.ordinal = index
    return performances


def sort_performances(performances: List[Performance]) -> List[Performance]:
    """
    Chronological, with a total ordering. The tiebreakers matter: without
    them, set iteration reorders equal-time events between runs and every
    daily commit shows a scrambled diff.
    """
    return sorted(performances,
                  key=lambda p: (p.sort_key, p.title.casefold(), p.venue_name, p.url))


# =============================================================================
# CLASSIFICATION
# =============================================================================

def _matches_any(text: str, patterns: List[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _map_group(value: str, groups: Dict[str, List[str]], default: str) -> str:
    """Map a messy real-world name onto a display group via alias lists."""
    lowered = value.lower().strip()
    for label, aliases in groups.items():
        for alias in aliases:
            if alias in lowered:
                return label
    return default


def classify(perf: Performance, config: dict) -> Performance:
    """Assign access, formats, region, groups and review flag. Mutates in place."""
    raw = perf.title
    lowered = raw.lower()

    # --- access: single-valued, first match by priority
    perf.access = config.get("access_default", "public")
    perf.classified_by = "default"
    for rule in sorted(config.get("access_rules", []), key=lambda r: r.get("priority", 100)):
        prefixes = rule.get("title_prefixes", [])
        if any(lowered.startswith(p) for p in prefixes) or \
           _matches_any(raw, rule.get("title_patterns", [])):
            perf.access = rule["id"]
            perf.classified_by = "keyword"
            break

    # --- display title: strip the access prefix for readability only
    perf.display_title = raw
    for rule in config.get("access_rules", []):
        for prefix in rule.get("title_prefixes", []):
            if lowered.startswith(prefix):
                perf.display_title = raw[len(prefix):].strip()
                break

    # --- needs_review: a restricted-sounding title that no access rule caught
    perf.needs_review = (
        perf.access == config.get("access_default", "public")
        and _matches_any(raw, config.get("needs_review_patterns", []))
    )

    # --- formats: multi-valued, all matches apply
    perf.formats = [r["id"] for r in config.get("format_rules", [])
                    if _matches_any(raw, r.get("title_patterns", []))]
    if not perf.formats:
        perf.formats = [config.get("format_default", "concert")]

    # --- performer: separate programme strands from real ensembles
    other = config.get("performer_other_label", "Other / various")
    programme_labels = config.get("programme_labels", [])
    non_performers = config.get("non_performers", [])

    if perf.performer in programme_labels:
        perf.programme = perf.performer
        perf.performer_group = other
    elif perf.performer in non_performers:
        perf.programme = None
        perf.performer_group = other
    else:
        perf.programme = None
        perf.performer_group = _map_group(
            perf.performer, config.get("performer_groups", {}), other)

    # --- venue grouping
    perf.venue_group = _map_group(
        perf.venue_name, config.get("venue_groups", {}),
        config.get("venue_other_label", "Other venues"))

    # --- region: venue name first, address second. lookup_venue_address()
    # defaults unknown venues to "Sydney, NSW", so the address alone lies.
    haystack = f"{perf.venue_name} {perf.venue_address}".lower()
    perf.region = ("outside" if _matches_any(haystack, config.get("outside_sydney_patterns", []))
                   else "sydney")

    # --- manual overrides win over everything
    overrides = config.get("overrides", {})
    ov = overrides.get(perf.event_id()) or overrides.get(perf.url)
    if ov:
        for attr in ("access", "formats", "needs_review", "performer_group", "region"):
            if attr in ov:
                setattr(perf, attr, ov[attr])
        perf.classified_by = "override"

    return perf


def daypart_of(perf: Performance, config: dict) -> str:
    """Which time-of-day band this performance falls into."""
    if not perf.time_known:
        return "tba"
    hour = perf.date.hour
    for band in config.get("dayparts", []):
        if band["from"] <= hour < band["to"]:
            return band["id"]
    return "evening"


# =============================================================================
# OUTPUT: events.json
# =============================================================================

def build_item(perf: Performance, config: dict) -> dict:
    """Serialise one performance for the frontend."""
    utc = sydney_to_utc(perf.date) if perf.time_known else None
    return {
        "id": perf.event_id(),
        "title": perf.display_title or perf.title,
        "raw_title": perf.title,
        "performer": perf.performer,
        "performer_group": perf.performer_group,
        "programme": perf.programme,
        "venue_name": perf.venue_name,
        "venue_group": perf.venue_group,
        "venue_address": perf.venue_address,
        "region": perf.region,
        "url": perf.url,
        "source": perf.source,
        "date": perf.date.strftime("%Y-%m-%d"),
        "time": perf.date.strftime("%H:%M") if perf.time_known else None,
        "start_utc": utc.strftime("%Y%m%dT%H%M%SZ") if utc else None,
        "time_confirmed": perf.time_known,
        "sort_key": perf.sort_key,
        "date_string": perf.date_str,
        "time_string": perf.time_str,
        "month_key": perf.date.strftime("%Y-%m"),
        "month_label": perf.date.strftime("%B %Y"),
        "weekday": perf.date.weekday(),          # 0 = Monday
        "is_weekend": perf.date.weekday() >= 5,
        "is_matinee": perf.is_matinee,
        "daypart": daypart_of(perf, config),
        "access": perf.access,
        "formats": perf.formats,
        "needs_review": perf.needs_review,
        "classified_by": perf.classified_by,
        "stale": perf.stale,
    }


def build_facets(items: List[dict], config: dict) -> dict:
    """
    Facet lists with labels and defaults, so index.html can build every
    filter group from data rather than duplicating definitions in JS.
    """
    def ordered_unique(values, other_label=None):
        seen = [v for v in sorted(set(values)) if v != other_label]
        if other_label and any(v == other_label for v in values):
            seen.append(other_label)   # "Other" always sorts last
        return seen

    access_facets = [{"id": config.get("access_default", "public"),
                      "label": config.get("access_public_label", "Publicly bookable"),
                      "default_on": True}]
    for rule in sorted(config.get("access_rules", []), key=lambda r: r.get("priority", 100)):
        access_facets.append({"id": rule["id"], "label": rule["label"],
                              "default_on": rule.get("default_on", False)})

    format_facets = [{"id": config.get("format_default", "concert"),
                      "label": config.get("format_default_label", "Concert"),
                      "default_on": True}]
    for rule in config.get("format_rules", []):
        format_facets.append({"id": rule["id"], "label": rule["label"],
                              "default_on": rule.get("default_on", True)})

    daypart_facets = [{"id": b["id"], "label": b["label"],
                       "default_on": True, "matinee": b["to"] <= MATINEE_CUTOFF_HOUR}
                      for b in config.get("dayparts", [])]
    daypart_facets.append({"id": "tba",
                           "label": config.get("daypart_tba_label", "Time TBA"),
                           "default_on": True, "matinee": False})

    weekday_labels = ["Monday", "Tuesday", "Wednesday", "Thursday",
                      "Friday", "Saturday", "Sunday"]

    return {
        "dayparts": daypart_facets,
        "access": access_facets,
        "formats": format_facets,
        "weekdays": [{"id": str(i), "label": lbl, "default_on": True,
                      "weekend": i >= 5} for i, lbl in enumerate(weekday_labels)],
        "performers": ordered_unique([i["performer_group"] for i in items],
                                     config.get("performer_other_label")),
        "venues": ordered_unique([i["venue_group"] for i in items],
                                 config.get("venue_other_label")),
        "regions": [{"id": "sydney", "label": "Greater Sydney", "default_on": True},
                    {"id": "outside", "label": "Outside Sydney", "default_on": False}],
        "sources": sorted(set(i["source"] for i in items)),
    }


def build_payload(performances: List[Performance], sources: List[dict],
                  config: dict) -> dict:
    """Assemble the full events.json structure."""
    items = [build_item(p, config) for p in performances]
    public = [i for i in items if i["access"] == "public"]
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": now_sydney().isoformat(timespec="seconds"),
        "generated_date": now_sydney().strftime("%Y-%m-%d"),
        "timezone": "Australia/Sydney",
        "counts": {
            "total": len(items),
            "public": len(public),
            "matinee": sum(1 for i in items if i["is_matinee"]),
            "public_matinee": sum(1 for i in public if i["is_matinee"]),
            "tba": sum(1 for i in items if not i["time_confirmed"]),
            "needs_review": sum(1 for i in items if i["needs_review"]),
            "stale": sum(1 for i in items if i["stale"]),
        },
        "facets": build_facets(items, config),
        "sources": sources,
        "items": items,
    }


def write_events_json(payload: dict, path) -> None:
    """Write events.json with a readable, reviewable daily diff."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False)
        f.write("\n")


# =============================================================================
# OUTPUT: .ics calendar feeds
# =============================================================================

def ics_escape(text: str) -> str:
    """Escape per RFC 5545: backslash, semicolon, comma, newline."""
    return (str(text).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def ics_fold(line: str) -> str:
    """
    Fold to 75 OCTETS per RFC 5545, not 75 characters.

    Folding by character count corrupts multi-byte sequences, and this data
    is full of them - curly apostrophes in "St James' King St", en-dashes in
    scraped titles, and names like Dvorak and Hakan Hardenberger.
    """
    if len(line.encode("utf-8")) <= 75:
        return line

    chunks, current, size = [], "", 0
    budget = 75          # first line may use all 75 octets
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > budget:
            chunks.append(current)
            current, size = ch, n
            budget = 74  # continuation lines spend one octet on the leading space
        else:
            current += ch
            size += n
    chunks.append(current)
    return "\r\n ".join(chunks)


def build_ics(items: List[dict], name: str, config: dict) -> str:
    """
    Build a calendar feed.

    Times are emitted in UTC rather than as a TZID reference: a TZID needs
    an embedded VTIMEZONE block to be RFC-compliant, and clients that can't
    resolve the identifier render the wrong time.
    """
    duration = timedelta(minutes=config.get("default_duration_minutes", 120))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Sydney Matinee Finder//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}",
        "X-WR-TIMEZONE:Australia/Sydney",
        "X-PUBLISHED-TTL:P1D",
        "REFRESH-INTERVAL;VALUE=DURATION:P1D",
    ]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for item in items:
        if not item.get("start_utc"):
            continue
        start = datetime.strptime(item["start_utc"], "%Y%m%dT%H%M%SZ")
        end = start + duration

        summary = item["title"]
        if item.get("performer_group") and item["performer_group"] not in ("Other / various",):
            summary = f"{item['title']} - {item['performer_group']}"

        description = (
            f"{item.get('performer', '')}\\n"
            f"Venue: {item.get('venue_name', '')}\\n"
            f"Confirm details and book: {item.get('url', '')}\\n"
            f"Listing source: {item.get('source', '')}. "
            f"Times and availability change - always confirm with the venue."
        )

        lines += [
            "BEGIN:VEVENT",
            f"UID:{item['id']}@matinee-finder",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{ics_escape(summary)}",
            f"LOCATION:{ics_escape(item.get('venue_name', '') + ', ' + item.get('venue_address', ''))}",
            f"DESCRIPTION:{ics_escape(description)}",
            f"URL:{item.get('url', '')}",
            f"CATEGORIES:{ics_escape(','.join(item.get('formats', [])))}",
            "TRANSP:TRANSPARENT",
            "STATUS:CONFIRMED",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(l) for l in lines) + "\r\n"


def feed_definitions(config: dict) -> List[dict]:
    """
    The published feeds. Each URL is a permanent contract with subscribers,
    so add sparingly and never rename.
    """
    return [
        {"file": "all.ics", "name": "Sydney Classical - All Concerts",
         "filter": lambda i: True},
        {"file": "matinees.ics", "name": "Sydney Classical - Matinees",
         "filter": lambda i: i["is_matinee"]},
        {"file": "weekend-matinees.ics", "name": "Sydney Classical - Weekend Matinees",
         "filter": lambda i: i["is_matinee"] and i["is_weekend"]},
    ]


def eligible_for_feeds(item: dict) -> bool:
    """
    Only publicly bookable, time-confirmed, Sydney events reach a calendar.
    A vague date must never land in someone's diary.
    """
    return (item.get("time_confirmed")
            and item.get("access") == "public"
            and item.get("region") == "sydney"
            and not item.get("stale"))


# =============================================================================
# SANITY
# =============================================================================

def sanity_check(items: List[dict], previous_path, config: dict) -> bool:
    """
    Refuse to overwrite good data with a collapsed dataset.

    Returning False means the caller writes nothing and exits non-zero, so
    the workflow goes red and emails rather than silently publishing a
    blank page.
    """
    import os
    if not os.path.exists(previous_path):
        return len(items) > 0
    try:
        with open(previous_path, encoding="utf-8") as f:
            prev = len(json.load(f).get("items", []))
    except (ValueError, OSError):
        return len(items) > 0

    ratio = config.get("global_sanity_ratio", 0.7)
    if prev >= 20 and len(items) < ratio * prev:
        print(f"  SANITY FAIL: item count collapsed {prev} -> {len(items)}. "
              "Site markup may have changed. Keeping existing data.")
        return False
    return True


def load_config(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
