# RDMNS Train Bot

A Telegram bot that searches Sri Lanka Railway timetables via the RDMNS WebView
API (`radar.hesn.xyz`).

> **No API key, no login, no capture file.**  The session key is generated
> automatically from the current time — the algorithm was reverse-engineered
> from the Flutter `libapp.so` binary.

## How the key works

```
key = SHA256( strftime("%Y%m%d%H%M", now_Asia/Colombo) + "RDMNS" ).hexdigest().upper()
```

The token is valid for exactly one minute (Sri Lanka local time, UTC+5:30).
Verified against two live HttpCanary captures:

| Capture | Minute (Asia/Colombo) | SHA-256 (first 16…) |
|---------|----------------------|---------------------|
| `ababa.zip` | `202606042021` | `239BD1AACBF039…` |
| `1.zip`     | `202606042106` | `6206016A602D03…` |

---

## Quick start

### 1 — Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

```bash
pip install uv          # or use the official installer
```

### 2 — Install

```bash
git clone <repo>
cd rdmns-train-bot
uv sync
```

### 3 — Configure

```bash
cp .env.example .env
```

Open `.env` and fill in your values:

| Variable | Required | Where to get it |
|----------|:--------:|-----------------|
| `TELEGRAM_API_ID` | ✅ | [my.telegram.org/apps](https://my.telegram.org/apps) |
| `TELEGRAM_API_HASH` | ✅ | [my.telegram.org/apps](https://my.telegram.org/apps) |
| `TELEGRAM_BOT_TOKEN` | ✅ | [@BotFather](https://t.me/BotFather) on Telegram |
| `RDMNS_DEVICE_ID` | optional | `adb shell settings get secure android_id` |

### 4 — Run

```bash
uv run python bot.py
# or via the installed entry-point:
uv run rdmns-bot
```

---

## Bot commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and usage overview |
| `/search` | Step-by-step guided train search |
| `/details <id>` | Full stop-by-stop schedule for a known train ID |

### Search flow

```
/search
  ↓  pick origin station  (inline keyboard or type a name)
  ↓  pick destination station
  ↓  pick travel date  (today + 6 days)
  ↓  train list  →  tap a train
  ↓  full stop schedule with live delay indicators
```

---

## Project layout

```
.
├── bot.py           # Telegram bot (Telethon)
├── timetable.py     # RDMNS HTTP client + HTML parsers
├── pyproject.toml   # uv project manifest + tooling config
├── uv.lock          # Pinned dependency tree
├── .env             # Your secrets (gitignored)
├── .env.example     # Template — safe to commit
└── .old/            # Original APK reversing artefacts
    └── targetapp/   # Decompiled app, HttpCanary captures, rdmns_timetable.py
```

---

## Development

```bash
# Lint
uv run ruff check .

# Type-check
uv run pyright
```

---

## Notes

- The first `/search` lazily bootstraps the RDMNS session (one extra HTTP
  round-trip, ~1–2 s).
- If the server returns `SESSION OUT`, the bot re-bootstraps automatically.
- The `RDMNS_DEVICE_ID` default is from the captured test device and works in
  practice; replace it with your own `android_id` if needed.
