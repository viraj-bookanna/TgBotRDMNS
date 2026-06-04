"""
timetable.py — RDMNS Sri Lanka Railway timetable API client.

This module encapsulates everything needed to query the RDMNS timetable API
(radar.hesn.xyz) without the Android app:

* **Session key generation** — reverse-engineered from ``libapp.so``::

      key = SHA256(strftime("%Y%m%d%H%M", now_Asia/Colombo) + "RDMNS").hexdigest().upper()

  The token is valid only for its minute (server accepts ±1 min tolerance).
  No API key, capture file, or ``app_config.json`` required.

* **Bootstrap** — replicates the WebView session flow::

      GET rdmns.hesn.xyz/timetbliso.php?key&did&lan
          → 302 radar.hesn.xyz/timetbliso.php
          → 302 n_timetablenew.php  (sets PHPSESSID cookie)

* **Search & detail** — HTML scrapers for ``timetablesearch.php`` and
  ``train.php`` responses.

Usage::

    client = RdmnsClient()
    client.bootstrap()
    trains = client.search_trains(500, "Colombo Fort", 46, "Rambukkana", date.today())
    detail = client.get_details(trains[0].tid)
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Base URL for all timetable HTML pages.
BASE_URL: str = "https://radar.hesn.xyz"

#: Entry-point URL that the app WebView first opens (triggers redirect chain).
TIMETBLISO_URL: str = "https://rdmns.hesn.xyz/app/timetbliso.php"

#: Android package identifier sent as ``x-requested-with`` header.
APP_PACKAGE: str = "com.rdmns24.chamiapps.rdmns24live"

#: Sri Lanka Standard Time (UTC +05:30).  No DST observed.
SL_TZ: timezone = timezone(timedelta(hours=5, minutes=30))

#: Literal suffix appended to the minute stamp before hashing.
KEY_SUFFIX: str = "RDMNS"

#: ``android_id`` from the captured device (``adb shell settings get secure android_id``).
DEFAULT_DEVICE_ID: str = "8b8c5ba96f2d5f5a"

#: WebView User-Agent as seen in HttpCanary captures.
USER_AGENT: str = (
    "Mozilla/5.0 (Linux; Android 15; 2409BRN2CA Build/AP3A.240905.015.A2; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/148.0.7778.178 "
    "Mobile Safari/537.36"
)

# Pre-compiled regexes used across parsers.
_TRAIN_LINK_RE: re.Pattern[str] = re.compile(r"train\.php\?tid=(\d+)", re.IGNORECASE)
_TRAIN_NUMBER_RE: re.Pattern[str] = re.compile(r"(\d+)\s*-\s*(.+)", re.DOTALL)
_TIME_RE: re.Pattern[str] = re.compile(
    r"^\s*\d{1,2}[:/]\d{2}\s*(AM|PM)\s*$", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def generate_api_key(when: Optional[datetime] = None) -> str:
    """Generate a valid RDMNS timetable session key for the given moment.

    The algorithm was recovered by reverse-engineering ``libapp.so`` (Flutter
    AOT, ``package:crypto/src/sha256.dart``) and verified against two
    HttpCanary captures:

    .. code-block:: text

        key = SHA256( "%Y%m%d%H%M"(Asia/Colombo) + "RDMNS" ).hexdigest().upper()

    The server honours only the **current minute** (±1 tolerance), so old or
    captured keys always return ``SESSION OUT``.

    Args:
        when: Point in time to generate the key for.  Defaults to ``now()``.
              Naive datetimes are assumed to already be in Sri Lanka local time.

    Returns:
        64-character uppercase hex digest string.

    Examples::

        >>> len(generate_api_key())
        64
        >>> generate_api_key(datetime(2026, 6, 4, 21, 6, 39, tzinfo=SL_TZ))
        '6206016A602D03A4A7947BAF5A9F152F17E707680C950AA81E4E2A06BBAFFF4D'
    """
    now: datetime = when if when is not None else datetime.now(SL_TZ)
    if now.tzinfo is None:
        # Treat naive datetimes as already in SL local time.
        now = now.replace(tzinfo=SL_TZ)
    else:
        now = now.astimezone(SL_TZ)
    stamp: str = now.strftime("%Y%m%d%H%M") + KEY_SUFFIX
    return hashlib.sha256(stamp.encode()).hexdigest().upper()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Station:
    """A railway station from the timetable dropdown.

    Attributes:
        sid: Numeric station ID used by the search API.
        name: Display name as it appears in the dropdown.
    """

    sid: int
    name: str


@dataclass
class TrainSummary:
    """Condensed information about a single train from a search result page.

    Attributes:
        tid: Internal train ID used by ``train.php?tid=``.
        headline: Human-readable route label (e.g. "Colombo Fort – Rambukkana").
        train_number: Official SLR train number (e.g. "1194").
        train_type: Service class string (e.g. "Slow", "Express").
        depart_time: Departure time string as shown in the timetable.
        arrive_time: Arrival time string as shown in the timetable.
        duration: Journey duration string (e.g. "2h 21m").
        operating_days: Days-of-week descriptor (e.g. "Mon–Sat").
    """

    tid: int
    headline: str = ""
    train_number: str = ""
    train_type: str = ""
    depart_time: str = ""
    arrive_time: str = ""
    duration: str = ""
    operating_days: str = ""

    def display_line(self) -> str:
        """Return a single-line human-readable summary suitable for a chat message."""
        parts: list[str] = []
        if self.train_number:
            parts.append(f"#{self.train_number}")
        if self.train_type:
            parts.append(self.train_type)
        if self.depart_time and self.arrive_time:
            parts.append(f"{self.depart_time} → {self.arrive_time}")
        if self.duration:
            parts.append(f"({self.duration})")
        return "  ".join(parts) if parts else f"Train {self.tid}"


@dataclass
class StationStop:
    """A single stop entry in a train's running schedule.

    Attributes:
        name: Station name.
        arrival: Scheduled arrival time string (may be empty for origin).
        departure: Scheduled departure time string (may be empty for terminus).
        live_status: Live delay / status text when available (may be empty).
    """

    name: str
    arrival: str = ""
    departure: str = ""
    live_status: str = ""


@dataclass
class TrainDetail:
    """Full schedule and metadata for one train.

    Attributes:
        tid: Train ID.
        title: Page title / route heading.
        train_number: Official SLR train number.
        train_type: Service class.
        operating_days: Days-of-week descriptor.
        origin: Name of the originating station.
        destination: Name of the terminating station.
        depart_time: Departure time from origin.
        arrive_time: Arrival time at destination.
        duration: Total journey duration.
        stops: Ordered list of all intermediate station stops.
    """

    tid: int
    title: str = ""
    train_number: str = ""
    train_type: str = ""
    operating_days: str = ""
    origin: str = ""
    destination: str = ""
    depart_time: str = ""
    arrive_time: str = ""
    duration: str = ""
    stops: list[StationStop] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionExpiredError(RuntimeError):
    """Raised when the server returns a ``SESSION OUT`` page.

    This happens when the session key is invalid or the ``PHPSESSID`` cookie
    has expired.  Callers should call :meth:`RdmnsClient.bootstrap` and retry.
    """


class NoTrainsFound(RuntimeError):
    """Raised when a search returns an empty result page.

    The route or date combination may simply have no scheduled services, or
    the search parameters could be wrong.
    """


# ---------------------------------------------------------------------------
# HTML parsing helpers (private)
# ---------------------------------------------------------------------------


def _text(el: Optional[Tag]) -> str:
    """Return normalised whitespace-joined text content of a BeautifulSoup tag.

    Args:
        el: A BeautifulSoup ``Tag``, or ``None``.

    Returns:
        Stripped inner text, or an empty string if *el* is ``None``.
    """
    return " ".join(el.stripped_strings) if el else ""


def _find_train_block(anchor: Tag) -> Optional[Tag]:
    """Walk up the DOM from an ``<span onclick="…train.php?tid=N…">`` element.

    The timetable search page wraps each train result in an unnamed container
    ``<div>`` that holds the headline, times, and the clickable span.  This
    function finds that container by walking ancestors until it reaches one
    that contains both a ``sinhalabld`` headline div and the same ``tid`` in
    an ``onclick`` span.

    Args:
        anchor: The ``<span>`` element whose ``onclick`` attribute references
                ``train.php?tid=N``.

    Returns:
        The enclosing block ``Tag``, or ``None`` if the structure is unexpected.
    """
    m = _TRAIN_LINK_RE.search(anchor.get("onclick", ""))
    if not m:
        return None
    tid: str = m.group(1)
    pat: re.Pattern[str] = re.compile(
        rf"train\.php\?tid={re.escape(tid)}(?:&|$)", re.IGNORECASE
    )
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        if not parent.find("div", class_="sinhalabld"):
            continue
        if parent.find("span", onclick=pat):
            return parent
    return None


def _fill_summary_from_block(summary: TrainSummary, block: Tag) -> None:
    """Populate a :class:`TrainSummary` from its enclosing HTML block.

    Mutates *summary* in place.

    Args:
        summary: The :class:`TrainSummary` instance to fill.
        block: The containing block ``Tag`` returned by :func:`_find_train_block`.
    """
    headline_div = block.find("div", class_="sinhalabld")
    if headline_div:
        summary.headline = _text(headline_div)

    # Train number line looks like "1194 - Slow".
    for div in block.find_all("div"):
        text = _text(div)
        if not text or len(text) > 80:
            continue
        nm = _TRAIN_NUMBER_RE.match(text)
        if nm and nm.group(1).isdigit() and len(nm.group(1)) <= 5:
            summary.train_number = nm.group(1).strip()
            summary.train_type = nm.group(2).strip()
            break

    days_div = block.find("div", class_="sinhalalight", align="right")
    if days_div:
        summary.operating_days = _text(days_div)

    time_spans: list[Tag] = block.find_all("span", string=_TIME_RE)
    if len(time_spans) >= 2:
        summary.depart_time = time_spans[0].get_text(strip=True)
        summary.arrive_time = time_spans[1].get_text(strip=True)

    for span in block.find_all("span", class_="englishfnt"):
        text = span.get_text(strip=True)
        if re.search(r"\d+h\s*\d+m", text, re.IGNORECASE):
            summary.duration = text
            break


# ---------------------------------------------------------------------------
# Public HTML parsers
# ---------------------------------------------------------------------------


def parse_stations(html: str) -> list[Station]:
    """Parse station ``<option>`` elements from the ``n_timetablenew.php`` page.

    The page contains two identical ``<select>`` dropdowns (origin/destination)
    each with ~430 stations.  We only need one.

    Args:
        html: Raw HTML of the timetable landing page.

    Returns:
        Sorted list of :class:`Station` (by name), excluding the placeholder.
    """
    soup = BeautifulSoup(html, "lxml")
    select = soup.find("select", attrs={"name": "sid1"})
    if not select:
        select = soup.find("select")
    stations: list[Station] = []
    if select:
        for opt in select.find_all("option"):
            val = (opt.get("value") or "").strip()
            name = opt.get_text(strip=True)
            if val and val.isdigit() and name:
                stations.append(Station(sid=int(val), name=name))
    stations.sort(key=lambda s: s.name.lower())
    return stations


def parse_search_results(html: str) -> list[TrainSummary]:
    """Parse the ``timetablesearch.php`` response HTML into train summaries.

    Args:
        html: Raw HTML string returned by the search endpoint.

    Returns:
        List of :class:`TrainSummary` objects sorted by departure time.
        An empty list means the page had no train entries (caller may raise
        :class:`NoTrainsFound`).
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[int] = set()
    trains: list[TrainSummary] = []

    for span in soup.find_all("span", onclick=True):
        m = _TRAIN_LINK_RE.search(span.get("onclick", ""))
        if not m:
            continue
        tid = int(m.group(1))
        if tid in seen:
            continue
        seen.add(tid)
        summary = TrainSummary(tid=tid)
        block = _find_train_block(span)
        if block:
            _fill_summary_from_block(summary, block)
        trains.append(summary)

    trains.sort(key=lambda t: (t.depart_time, t.tid))
    return trains


def parse_train_detail(html: str, tid: int) -> TrainDetail:
    """Parse a ``train.php`` response HTML into a full :class:`TrainDetail`.

    Args:
        html: Raw HTML string returned by ``train.php?tid=N&lan=en``.
        tid: The train ID that was queried (used to populate :attr:`TrainDetail.tid`).

    Returns:
        :class:`TrainDetail` with all fields extracted from the page.
        Stops are deduplicated by skipping rows with no arrival or departure time.
    """
    soup = BeautifulSoup(html, "lxml")
    detail = TrainDetail(tid=tid)

    title_div = soup.find(
        "div",
        class_="sinhalabld",
        style=re.compile(r"font-size:\s*20px", re.IGNORECASE),
    )
    if title_div:
        detail.title = _text(title_div)

    sub_div = soup.find(
        "div",
        class_="sinhalalight",
        style=re.compile(r"font-size:\s*16px", re.IGNORECASE),
    )
    if sub_div:
        m = _TRAIN_NUMBER_RE.match(_text(sub_div))
        if m:
            detail.train_number = m.group(1).strip()
            detail.train_type = m.group(2).strip()

    days_div = soup.find(
        "div",
        class_="sinhalalight",
        align="right",
        style=re.compile(r"font-size:\s*12px", re.IGNORECASE),
    )
    if days_div:
        detail.operating_days = _text(days_div)

    # Paired span IDs for origin/destination names and times.
    for span_id, attr_name in (("inst", "origin"), ("outst", "destination")):
        el = soup.find("span", id=span_id)
        if el:
            setattr(detail, attr_name, el.get_text(strip=True))

    for base_id, attr_name in (("intime", "depart_time"), ("outtime", "arrive_time")):
        el = soup.find("span", id=base_id)
        if el:
            ampm = soup.find("span", id=base_id + "a")
            val = el.get_text(strip=True)
            if ampm:
                val = f"{val} {ampm.get_text(strip=True)}"
            setattr(detail, attr_name, val)

    timecal = soup.find("span", id="timecal")
    if timecal:
        detail.duration = _text(timecal)

    running_div = soup.find("div", class_="runningtrain")
    if running_div:
        for row in running_div.find_all("tr"):
            station_cell = row.find("td", class_="sinhalabld")
            if not station_cell:
                continue
            name = _text(station_cell)
            if not name:
                continue
            time_cells: list[Tag] = row.find_all("td", class_="englishfnt")
            arrival = departure = ""
            if len(time_cells) >= 2:
                arrival = time_cells[0].get_text(strip=True)
                departure = time_cells[1].get_text(strip=True)
            if not arrival and not departure:
                continue  # skip header rows that carry no schedule data
            live_span = row.find("span", class_="runningtraintext")
            detail.stops.append(
                StationStop(
                    name=name,
                    arrival=arrival,
                    departure=departure,
                    live_status=_text(live_span) if live_span else "",
                )
            )

    return detail


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class RdmnsClient:
    """HTTP client for the RDMNS timetable API (radar.hesn.xyz).

    Each instance maintains its own :class:`requests.Session` so cookies
    (``PHPSESSID``, ``selectedOption``, ``selectedOption2``) are persisted
    between calls.

    The session key is regenerated on every :meth:`bootstrap` call so the
    client remains valid across minute boundaries without any manual renewal.

    Args:
        device_id: 16-character hex ``android_id`` sent as the ``did`` query
            parameter.  Defaults to the captured device ID.
        lang: Language code sent as the ``lan`` query parameter (``"en"`` or
            ``"si"``).
        timeout: Default socket timeout in seconds for all requests.

    Example::

        client = RdmnsClient()
        client.bootstrap()
        trains = client.search_trains(500, "Colombo Fort", 46, "Rambukkana", date.today())
        for t in trains:
            print(t.display_line())
    """

    def __init__(
        self,
        device_id: str = DEFAULT_DEVICE_ID,
        lang: str = "en",
        timeout: int = 30,
    ) -> None:
        self.device_id: str = device_id.strip().lower()
        self.lang: str = lang
        self.timeout: int = timeout
        self.stations: list[Station] = []
        self._http: requests.Session = requests.Session()
        self._http.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "x-requested-with": APP_PACKAGE,
            }
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Establish a valid PHPSESSID cookie by replaying the WebView entry flow.

        The flow is:

        1. ``GET rdmns.hesn.xyz/app/timetbliso.php?key=…&did=…&lan=en``
        2. Server redirects through ``radar.hesn.xyz`` and lands on
           ``n_timetablenew.php``, setting the ``PHPSESSID`` cookie.

        A fresh session key is computed for the current minute before each
        attempt.  Call this method once before any searches; call it again if
        :class:`SessionExpiredError` is raised.

        Raises:
            SessionExpiredError: If the final response page contains
                ``SESSION OUT`` (indicates a clock skew or server-side issue).
            requests.HTTPError: On unexpected non-2xx responses.
        """
        key = generate_api_key()
        params: dict[str, str] = {
            "key": key,
            "did": self.device_id,
            "lan": self.lang,
        }
        log.debug("bootstrap key=%s… did=%s", key[:8], self.device_id)
        resp = self._http.get(
            TIMETBLISO_URL,
            params=params,
            allow_redirects=True,
            timeout=self.timeout,
        )
        if "SESSION OUT" in (resp.text or "").upper():
            raise SessionExpiredError(
                "Bootstrap failed: SESSION OUT.  Check that your system clock is "
                "accurate (key is based on Sri Lanka local time)."
            )

        # Fetch timetable page to extract the full station list.
        key = generate_api_key()
        timetable_resp = self._http.get(
            f"{BASE_URL}/app/n_timetablenew.php",
            params={"did": self.device_id, "key": key, "lan": self.lang},
            timeout=self.timeout,
        )
        stations = parse_stations(timetable_resp.text or "")
        if stations:
            self.stations = stations
            log.info("Loaded %d stations from timetable page.", len(stations))

    def search_trains(
        self,
        from_id: int,
        from_name: str,
        to_id: int,
        to_name: str,
        travel_date: date | str,
    ) -> list[TrainSummary]:
        """Search for trains between two stations on a given date.

        The method sets ``selectedOption`` / ``selectedOption2`` cookies to
        match the behaviour of the WebView front-end.

        Args:
            from_id: Origin station numeric ID (e.g. ``500`` for Colombo Fort).
            from_name: Origin station display name (e.g. ``"Colombo Fort"``).
            to_id: Destination station numeric ID.
            to_name: Destination station display name.
            travel_date: Date of travel as a :class:`~datetime.date` or an
                ISO-8601 string (``"YYYY-MM-DD"``).

        Returns:
            List of :class:`TrainSummary` sorted by departure time.

        Raises:
            NoTrainsFound: If the search page returns no train entries.
            SessionExpiredError: If the response contains ``SESSION OUT``.
            requests.HTTPError: On HTTP errors.
        """
        dt: str = (
            travel_date.isoformat()
            if isinstance(travel_date, date)
            else str(travel_date)
        )

        self._http.cookies.set("selectedOption", str(from_id), domain="radar.hesn.xyz")
        self._http.cookies.set("selectedOption2", str(to_id), domain="radar.hesn.xyz")

        key = generate_api_key()
        referer = (
            f"{BASE_URL}/app/n_timetablenew.php"
            f"?did={self.device_id}&key={key}&lan={self.lang}"
        )
        params: dict[str, str] = {
            "frm": str(from_id),
            "frmnm": from_name,
            "to": str(to_id),
            "tonm": to_name,
            "dt": dt,
        }
        log.debug(
            "search frm=%s to=%s date=%s", from_id, to_id, dt
        )
        resp = self._http.get(
            f"{BASE_URL}/app/timetablesearch.php",
            params=params,
            headers={"Referer": referer},
            timeout=self.timeout,
        )

        html = resp.text or ""
        if "SESSION OUT" in html.upper():
            raise SessionExpiredError(
                "Search returned SESSION OUT.  Call bootstrap() first."
            )

        trains = parse_search_results(html)
        if not trains:
            raise NoTrainsFound(
                f"No trains found for {from_name} → {to_name} on {dt}."
            )
        return trains

    def get_details(self, tid: int) -> TrainDetail:
        """Fetch the full stop-by-stop schedule for a specific train.

        Args:
            tid: Train ID as returned by :meth:`search_trains`.

        Returns:
            :class:`TrainDetail` with all stops populated.

        Raises:
            SessionExpiredError: If the response contains ``SESSION OUT``.
            requests.HTTPError: On HTTP errors.
        """
        log.debug("get_details tid=%s", tid)
        resp = self._http.get(
            f"{BASE_URL}/app/train.php",
            params={"tid": str(tid), "lan": self.lang},
            timeout=self.timeout,
        )
        html = resp.text or ""
        if "SESSION OUT" in html.upper():
            raise SessionExpiredError(
                "Details returned SESSION OUT.  Call bootstrap() first."
            )
        return parse_train_detail(html, tid=tid)
