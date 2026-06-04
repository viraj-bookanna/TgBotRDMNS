"""
bot.py — RDMNS Telegram bot (Telethon).

Provides an interactive timetable search for Sri Lanka Railway via the RDMNS
WebView API (radar.hesn.xyz).  The session key is generated automatically on
every bootstrap — no capture file or API key is required.

Bot commands
------------
/start         — Welcome message and usage overview.
/search        — Guided multi-step search:
                     1. Pick origin station (inline keyboard or free text)
                     2. Pick destination station
                     3. Pick travel date (today + next 6 days)
                     4. Browse train list → tap for full stop schedule
/details <tid> — Jump directly to the stop schedule for a known train ID.

Running
-------
Configure ``.env`` (see ``.env.example``), then::

    uv run python bot.py

Environment variables
---------------------
TELEGRAM_API_ID     — Telegram application ID (https://my.telegram.org/apps).
TELEGRAM_API_HASH   — Telegram application hash.
TELEGRAM_BOT_TOKEN  — Bot token from @BotFather.
RDMNS_DEVICE_ID     — Optional; Android ``android_id`` for the ``did`` param.
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from datetime import date, timedelta
from typing import Any, Final

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from telethon.events import CallbackQuery, NewMessage

from timetable import (
    NoTrainsFound,
    RdmnsClient,
    SessionExpiredError,
    TrainDetail,
    TrainSummary,
    generate_api_key,
    SL_TZ,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log: logging.Logger = logging.getLogger("rdmns_bot")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

API_ID: int = int(os.environ["TELEGRAM_API_ID"])
API_HASH: str = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
DEVICE_ID: str = os.environ.get("RDMNS_DEVICE_ID", "8b8c5ba96f2d5f5a")

# ---------------------------------------------------------------------------
# Station catalogue
# ---------------------------------------------------------------------------

#: Name → (station_id, canonical_name)  mapping used in the quick-pick keyboard.
STATIONS: Final[dict[str, tuple[int, str]]] = {
    "Colombo Fort":    (500, "Colombo Fort"),
    "Maradana":        (501, "Maradana"),
    "Ragama":          (503, "Ragama"),
    "Gampaha":         (505, "Gampaha"),
    "Veyangoda":       (507, "Veyangoda"),
    "Polgahawela":     (42,  "Polgahawela"),
    "Rambukkana":      (46,  "Rambukkana"),
    "Kandy":           (50,  "Kandy"),
    "Peradeniya Jn.":  (51,  "Peradeniya Jn."),
    "Badulla":         (83,  "Badulla"),
    "Galle":           (305, "Galle"),
    "Matara":          (312, "Matara"),
    "Anuradhapura":    (254, "Anuradhapura"),
    "Jaffna":          (280, "Jaffna"),
    "Batticaloa":      (360, "Batticaloa"),
    "Trincomalee":     (352, "Trincomalee"),
    "Kurunegala":      (167, "Kurunegala"),
    "Negombo":         (502, "Negombo"),
    "Panadura":        (205, "Panadura"),
    "Kalutara South":  (210, "Kalutara South"),
}

_STATION_NAMES: list[str] = sorted(STATIONS)

# ---------------------------------------------------------------------------
# Telethon client + shared RDMNS client
# ---------------------------------------------------------------------------

bot: TelegramClient = TelegramClient("rdmns_bot_session", API_ID, API_HASH)
rdmns: RdmnsClient = RdmnsClient(device_id=DEVICE_ID)

# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------

# Keyed by Telegram user ID.  Each entry holds the current step and any
# collected inputs for the ongoing /search conversation.
#
# Shape:
#   {
#     "step":      "from" | "to" | "date" | "searching" | "results",
#     "from_id":   int,
#     "from_name": str,
#     "to_id":     int,
#     "to_name":   str,
#     "date":      date,
#     "trains":    list[TrainSummary],
#   }
_state: dict[int, dict[str, Any]] = {}

# Whether the shared RdmnsClient session has been bootstrapped at least once.
_session_ready: bool = False

# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------


def _station_keyboard() -> list[list[Button]]:
    """Build a 3-column inline keyboard for the station quick-pick grid.

    Returns:
        Nested list of :class:`Button` rows accepted by Telethon's ``buttons``
        parameter.
    """
    rows: list[list[Button]] = []
    for i in range(0, len(_STATION_NAMES), 3):
        rows.append(
            [Button.inline(n, data=f"st:{n}") for n in _STATION_NAMES[i : i + 3]]
        )
    return rows


def _date_keyboard() -> list[list[Button]]:
    """Build a 2-column inline keyboard for the next 7 days.

    Returns:
        Nested list of :class:`Button` rows.
    """
    today = date.today()
    rows: list[list[Button]] = []
    row: list[Button] = []
    for offset in range(7):
        d = today + timedelta(days=offset)
        label = d.strftime("%d %b") + ("  (today)" if offset == 0 else "")
        row.append(Button.inline(label, data=f"dt:{d.isoformat()}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _train_list_keyboard(trains: list[TrainSummary]) -> list[list[Button]]:
    """Build an inline keyboard where each row is one train from a search result.

    Args:
        trains: List of :class:`TrainSummary` to display (capped at 15).

    Returns:
        Nested list of :class:`Button` rows, with a "New search" button appended.
    """
    rows: list[list[Button]] = []
    for t in trains[:15]:
        label = f"{t.depart_time or '?'}  {t.headline or t.train_number or str(t.tid)}"
        rows.append([Button.inline(label[:50], data=f"tid:{t.tid}")])
    rows.append([Button.inline("🔄 New search", data="cmd:search")])
    return rows


# ---------------------------------------------------------------------------
# Message formatters
# ---------------------------------------------------------------------------


def _fmt_train_list(
    trains: list[TrainSummary],
    from_name: str,
    to_name: str,
    travel_date: date,
) -> str:
    """Format a numbered train list as a Markdown string.

    Args:
        trains: Search results to render (capped at 15).
        from_name: Origin station display name.
        to_name: Destination station display name.
        travel_date: The queried travel date.

    Returns:
        Multi-line Markdown-formatted string.
    """
    header = (
        f"🚉 **{from_name} → {to_name}**\n"
        f"📅 {travel_date.strftime('%A, %d %B %Y')}  —  {len(trains)} train(s)\n\n"
    )
    lines: list[str] = []
    for i, t in enumerate(trains[:15], start=1):
        headline = t.headline or f"Train {t.tid}"
        num_type = f"#{t.train_number} {t.train_type}".strip()
        time_part = (
            f"  🕐 {t.depart_time} → {t.arrive_time}" if t.depart_time else ""
        )
        dur = f"  ({t.duration})" if t.duration else ""
        lines.append(f"**{i}.** {headline}\n    {num_type}{time_part}{dur}")
    return header + "\n\n".join(lines) + "\n\n_Tap a train to see its full schedule._"


def _fmt_train_detail(detail: TrainDetail) -> str:
    """Format a :class:`TrainDetail` as a Markdown string.

    Args:
        detail: The :class:`TrainDetail` to render.

    Returns:
        Multi-line Markdown-formatted string including the stop table.
    """
    lines: list[str] = []

    title = detail.title or f"Train {detail.tid}"
    lines.append(f"🚂 **{title}**")

    if detail.train_number:
        lines.append(f"Train **#{detail.train_number}**  {detail.train_type}".rstrip())

    if detail.operating_days:
        lines.append(f"📅 Runs: {detail.operating_days}")

    if detail.origin or detail.destination:
        lines.append(f"🛤 {detail.origin or '?'} → {detail.destination or '?'}")

    if detail.depart_time or detail.arrive_time:
        lines.append(
            f"🕐 Departs **{detail.depart_time}**  |  Arrives **{detail.arrive_time}**"
            + (f"  ({detail.duration})" if detail.duration else "")
        )

    if detail.stops:
        lines.append(f"\n**Schedule — {len(detail.stops)} stops:**")
        for stop in detail.stops:
            dep = stop.departure or stop.arrival
            live = f"  _{stop.live_status}_" if stop.live_status else ""
            lines.append(f"  • {stop.name}  `{dep}`{live}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------


async def _ensure_session() -> None:
    """Bootstrap the RDMNS HTTP session if it has not been set up yet.

    This is called lazily before the first search rather than at startup so
    the bot is responsive immediately even if the upstream server is slow.

    Raises:
        SessionExpiredError: Propagated from :meth:`RdmnsClient.bootstrap`.
    """
    global _session_ready
    if not _session_ready:
        log.info("Bootstrapping RDMNS session …")
        await asyncio.get_event_loop().run_in_executor(None, rdmns.bootstrap)
        _session_ready = True
        log.info("RDMNS session ready.")


async def _rebootstrap() -> None:
    """Force a fresh bootstrap (used after :class:`SessionExpiredError`)."""
    global _session_ready
    _session_ready = False
    await _ensure_session()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


@bot.on(events.NewMessage(pattern=r"(?i)^/start(@\w+)?$"))
async def handle_start(event: NewMessage.Event) -> None:
    """Handle the ``/start`` command — send the welcome message.

    Args:
        event: Incoming message event.
    """
    await event.respond(
        "🚂 **RDMNS Train Bot**\n\n"
        "Search Sri Lanka Railway timetables.\n\n"
        "**Commands**\n"
        "/search — find trains (step-by-step guide)\n"
        "/details `<id>` — show the schedule for a train by ID\n\n"
        "_The session key is generated automatically from the current time — "
        "no API key or login needed._",
        parse_mode="md",
    )


@bot.on(events.NewMessage(pattern=r"(?i)^/search(@\w+)?$"))
async def handle_search(event: NewMessage.Event) -> None:
    """Handle the ``/search`` command — begin the guided search flow.

    Resets any existing state for the user and asks for the origin station.

    Args:
        event: Incoming message event.
    """
    uid: int = event.sender_id
    _state[uid] = {"step": "from"}
    await event.respond(
        "🛤 **Where are you travelling FROM?**\n"
        "Pick a station below, or type a station name:",
        buttons=_station_keyboard(),
        parse_mode="md",
    )


@bot.on(events.NewMessage(pattern=r"(?i)^/details(@\w+)?\s+(\d+)$"))
async def handle_details(event: NewMessage.Event) -> None:
    """Handle ``/details <tid>`` — fetch and display a train's full schedule.

    Args:
        event: Incoming message event; the train ID is captured in group 2.
    """
    tid = int(event.pattern_match.group(2))
    status_msg = await event.respond(f"⏳ Fetching schedule for train **{tid}** …", parse_mode="md")
    try:
        await _ensure_session()
        detail = await asyncio.get_event_loop().run_in_executor(
            None, rdmns.get_details, tid
        )
        await status_msg.edit(
            _fmt_train_detail(detail),
            parse_mode="md",
            buttons=[[Button.inline("🔄 New search", data="cmd:search")]],
        )
    except SessionExpiredError:
        await _rebootstrap()
        await status_msg.edit("⚠️ Session expired — please try again.")
    except Exception:
        log.error(traceback.format_exc())
        await status_msg.edit("❌ Something went wrong fetching that train.")


@bot.on(events.NewMessage(pattern=r"(?i)^/details(@\w+)?$"))
async def handle_details_no_arg(event: NewMessage.Event) -> None:
    """Handle ``/details`` with no argument — prompt the user for the train ID.

    Args:
        event: Incoming message event.
    """
    await event.respond("Usage: `/details <train_id>`  e.g. `/details 43`", parse_mode="md")


# ---------------------------------------------------------------------------
# Free-text input (station name typed by the user)
# ---------------------------------------------------------------------------


@bot.on(events.NewMessage)
async def handle_text(event: NewMessage.Event) -> None:
    """Handle free-text messages during an active search conversation.

    Only acts when the user is in the ``"from"`` or ``"to"`` step and has
    typed something that is not a bot command.

    Args:
        event: Incoming message event.
    """
    if not event.is_private or not event.raw_text:
        return
    text = event.raw_text.strip()
    if text.startswith("/"):
        return

    uid: int = event.sender_id
    user_state = _state.get(uid)
    if not user_state or user_state.get("step") not in ("from", "to"):
        return

    query = text.lower()
    matches = [n for n in _STATION_NAMES if query in n.lower()]

    if len(matches) == 1:
        name = matches[0]
        sid, sname = STATIONS[name]
        await _assign_station(event, uid, user_state, user_state["step"], sid, sname)
    elif len(matches) > 1:
        buttons = [[Button.inline(n, data=f"st:{n}")] for n in matches[:10]]
        await event.respond(
            f'Multiple stations match "{text}" — pick one:',
            buttons=buttons,
        )
    else:
        await event.respond(
            f'No station matched **"{text}"**.  Try picking from the list:',
            buttons=_station_keyboard(),
            parse_mode="md",
        )


# ---------------------------------------------------------------------------
# Callback query handler (all inline button presses)
# ---------------------------------------------------------------------------


@bot.on(events.CallbackQuery)
async def handle_callback(event: CallbackQuery.Event) -> None:
    """Dispatch all inline-button callbacks to the appropriate handler.

    The ``data`` field encodes the action as a colon-prefixed token:

    * ``cmd:search``  — restart search flow
    * ``cmd:back``    — return to the train list
    * ``st:<name>``   — station selected from keyboard
    * ``dt:<iso>``    — date selected from keyboard
    * ``tid:<id>``    — train selected from list

    Args:
        event: Callback query event triggered by an inline button press.
    """
    uid: int = event.sender_id
    data: str = event.data.decode("utf-8")

    # ── new search ────────────────────────────────────────────────────────
    if data == "cmd:search":
        _state[uid] = {"step": "from"}
        await event.edit(
            "🛤 **Where are you travelling FROM?**\n"
            "Pick a station below, or type a station name:",
            buttons=_station_keyboard(),
            parse_mode="md",
        )
        return

    # ── back to results list ──────────────────────────────────────────────
    if data == "cmd:back":
        s = _state.get(uid)
        if s and s.get("trains"):
            trains: list[TrainSummary] = s["trains"]
            await event.edit(
                _fmt_train_list(trains, s["from_name"], s["to_name"], s["date"]),
                parse_mode="md",
                buttons=_train_list_keyboard(trains),
            )
        else:
            await event.answer("No results cached — start a new /search.", alert=True)
        return

    # ── station selected ──────────────────────────────────────────────────
    if data.startswith("st:"):
        station_name = data[3:]
        s = _state.get(uid, {"step": "from"})
        step = s.get("step")
        if step not in ("from", "to"):
            await event.answer("Use /search to start a new search.", alert=True)
            return
        sid, sname = STATIONS.get(station_name, (0, station_name))
        await _assign_station(event, uid, s, step, sid, sname, is_callback=True)
        return

    # ── date selected ─────────────────────────────────────────────────────
    if data.startswith("dt:"):
        s = _state.get(uid)
        if not s or s.get("step") != "date":
            await event.answer("Use /search to start a new search.", alert=True)
            return
        s["date"] = date.fromisoformat(data[3:])
        s["step"] = "searching"
        await event.edit(
            f"🔍 Searching **{s['from_name']}** → **{s['to_name']}** "
            f"on **{s['date'].strftime('%d %B %Y')}** …",
            parse_mode="md",
        )
        await _do_search(event, uid, s)
        return

    # ── train selected ────────────────────────────────────────────────────
    if data.startswith("tid:"):
        tid = int(data[4:])
        await event.edit(f"⏳ Loading schedule for train **{tid}** …", parse_mode="md")
        try:
            await _ensure_session()
            detail = await asyncio.get_event_loop().run_in_executor(
                None, rdmns.get_details, tid
            )
            await event.edit(
                _fmt_train_detail(detail),
                parse_mode="md",
                buttons=[
                    [
                        Button.inline("◀ Back to list", data="cmd:back"),
                        Button.inline("🔄 New search", data="cmd:search"),
                    ]
                ],
            )
        except SessionExpiredError:
            await _rebootstrap()
            await event.edit(
                "⚠️ Session expired — please retry.",
                buttons=[[Button.inline(f"Retry train {tid}", data=data)]],
            )
        except Exception:
            log.error(traceback.format_exc())
            await event.edit("❌ Something went wrong fetching that train.")
        return

    # Unrecognised callback — acknowledge silently.
    await event.answer()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _assign_station(
    event: Any,
    uid: int,
    user_state: dict[str, Any],
    step: str,
    sid: int,
    sname: str,
    *,
    is_callback: bool = False,
) -> None:
    """Persist a station selection and advance the conversation.

    If *step* is ``"from"``, store the origin and ask for the destination.
    If *step* is ``"to"``, store the destination and ask for the date.

    Args:
        event: The originating event (used to send/edit the reply).
        uid: Telegram user ID.
        user_state: The mutable state dict for this user.
        step: Either ``"from"`` or ``"to"``.
        sid: Numeric station ID.
        sname: Canonical station name string.
        is_callback: ``True`` when responding to a :class:`CallbackQuery.Event`
            (uses ``event.edit``), ``False`` for a :class:`NewMessage.Event`
            (uses ``event.respond``).
    """
    send = event.edit if is_callback else event.respond

    if step == "from":
        user_state["from_id"] = sid
        user_state["from_name"] = sname
        user_state["step"] = "to"
        await send(
            f"✅ From: **{sname}**\n\n"
            "🛤 **Where are you travelling TO?**\n"
            "Pick a station below, or type a station name:",
            buttons=_station_keyboard(),
            parse_mode="md",
        )

    elif step == "to":
        if sid == user_state.get("from_id"):
            msg = "⚠️ Origin and destination cannot be the same. Pick a different station:"
            if is_callback:
                await event.answer(msg, alert=True)
            else:
                await send(msg, buttons=_station_keyboard())
            return

        user_state["to_id"] = sid
        user_state["to_name"] = sname
        user_state["step"] = "date"
        await send(
            f"✅ To: **{sname}**\n\n📅 **Pick a travel date:**",
            buttons=_date_keyboard(),
            parse_mode="md",
        )


async def _do_search(event: Any, uid: int, user_state: dict[str, Any]) -> None:
    """Execute the train search and display results.

    Runs the blocking network call in a thread-pool executor, then updates the
    message in-place.

    Args:
        event: The originating event (used to call ``event.edit``).
        uid: Telegram user ID (used to persist results in *_state*).
        user_state: The mutable state dict for this user.
    """
    global _session_ready
    try:
        await _ensure_session()
        trains = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: rdmns.search_trains(
                user_state["from_id"],
                user_state["from_name"],
                user_state["to_id"],
                user_state["to_name"],
                user_state["date"],
            ),
        )
        user_state["trains"] = trains
        user_state["step"] = "results"
        await event.edit(
            _fmt_train_list(
                trains,
                user_state["from_name"],
                user_state["to_name"],
                user_state["date"],
            ),
            parse_mode="md",
            buttons=_train_list_keyboard(trains),
        )

    except SessionExpiredError:
        _session_ready = False
        await event.edit(
            "⚠️ Session expired — please try /search again."
        )
    except NoTrainsFound:
        await event.edit(
            f"😕 No trains found for **{user_state['from_name']} → "
            f"{user_state['to_name']}** on "
            f"{user_state['date'].strftime('%d %B %Y')}.",
            parse_mode="md",
            buttons=[[Button.inline("🔄 New search", data="cmd:search")]],
        )
    except Exception:
        log.error(traceback.format_exc())
        await event.edit("❌ Search failed.  Please try again later.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the bot and run until interrupted.

    Connects using the bot token, sets up all event handlers, and runs the
    Telethon event loop.  Press Ctrl-C to stop gracefully.
    """
    log.info("Starting RDMNS Train Bot …")
    bot.start(bot_token=BOT_TOKEN)
    log.info(
        "Bot running.  Key algorithm: SHA256(minute[Asia/Colombo] + 'RDMNS')  "
        "— no capture needed.  Press Ctrl-C to stop."
    )
    bot.run_until_disconnected()


if __name__ == "__main__":
    main()
