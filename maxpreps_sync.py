#!/usr/bin/env python3
"""
MaxPreps -> concession-stand calendar feed.

Reads MaxPreps team schedule pages, writes an .ics feed (one "Concessions"
event per home-game day) and reports any schedule changes since the last run.

Usage:
    python maxpreps_sync.py            # normal run
    python maxpreps_sync.py --dry-run  # parse + print, write nothing, no alerts
Env:
    NOTIFY_WEBHOOK  optional URL; changes are POSTed as JSON {"title","message"}
                    (e.g. a Home Assistant webhook automation)
"""
import json, os, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ----------------------------- CONFIG ---------------------------------
BASE = "https://www.maxpreps.com/fl/high-springs/first-christian-lions/"
TEAMS = [
    # (label shown in calendar, MaxPreps schedule URL) - add a line per team, any sport
    ("VB Varsity", BASE + "volleyball/schedule/"),
    ("VB MS",      BASE + "volleyball/jv/schedule/"),
    ("BB Boys V",   BASE + "basketball/schedule/"),
    ("BB Boys MS",  BASE + "basketball/jv/schedule/"),
    ("BB Girls V",  BASE + "basketball/girls/schedule/"),
    ("BB Girls MS", BASE + "basketball/girls/jv/schedule/"),
]
LEAD_MINUTES = 120         # how long before the FIRST game she needs to be there
TAIL_MINUTES = 120         # event ends this long after the LAST game's start time
INCLUDE_AWAY = False       # away games never go on the calendar, but changes to them are ignored too
HOME_LOCATION = "First Christian, 24530 NW 199th Ln, High Springs, FL 32643"
TZ = ZoneInfo("America/New_York")
OUT_ICS = Path("docs/concessions.ics")
STATE = Path("state.json")
# -----------------------------------------------------------------------

# contest links look like .../volleyball/match/<slug>/9-18-2026/?c=<id> or .../basketball/game/<slug>/jv/11-2-2026/?c=<id>
MATCH_RE = re.compile(r"/(\d{1,2})-(\d{1,2})-(\d{4})/\?c=([0-9a-fA-F-]{36})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([ap])m", re.I)
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                         "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
           "Accept-Language": "en-US,en;q=0.9"}


def clean_opponent(text):
    """'vs J JV Opponent *' -> (True, 'JV Opponent')"""
    t = " ".join(text.replace("*", " ").split())
    m = re.match(r"^(vs\.?|@)\s*(.*)$", t, re.I)
    if not m:
        return None, t
    home = m.group(1).lower().startswith("vs")
    name = m.group(2).strip()
    # MaxPreps shows a one-letter avatar when a school has no mascot image
    parts = name.split(" ", 1)
    if len(parts) == 2 and len(parts[0]) == 1 and parts[1][:1].upper() == parts[0].upper():
        name = parts[1]
    return home, name


def parse_schedule(html, team):
    soup = BeautifulSoup(html, "html.parser")
    games = {}
    for a in soup.find_all("a", href=MATCH_RE):
        m = MATCH_RE.search(a["href"])
        mo, d, y, cid = int(m[1]), int(m[2]), int(m[3]), m[4].lower()
        if cid in games:
            continue
        row = a.find_parent("tr") or a.find_parent("li") or a.parent.parent
        row_text = row.get_text(" ", strip=True)
        tm = TIME_RE.search(a.get_text(" ", strip=True)) or TIME_RE.search(row_text)
        time_str = None
        if tm:
            h = int(tm[1]) % 12 + (12 if tm[3].lower() == "p" else 0)
            time_str = f"{h:02d}:{tm[2]}"
        home, opp = None, "TBD"
        for link in row.find_all("a"):
            if MATCH_RE.search(link.get("href", "")):
                continue
            h_, o_ = clean_opponent(link.get_text(" ", strip=True))
            if h_ is not None:
                home, opp = h_, o_
                break
        if home is None:  # fall back to scanning the whole row
            mm = re.search(r"(?:^|\s)(vs\.?|@)\s", row_text)
            if mm:
                home = mm.group(1).lower().startswith("vs")
        # "vs" + "(Location: somewhere else)" = neutral-site game, not a home game
        loc = re.search(r"Location:\s*([^)]*)", row_text)
        if home and loc and "first christian" not in loc.group(1).lower():
            home = False
        games[cid] = {"team": team, "date": f"{y:04d}-{mo:02d}-{d:02d}",
                      "time": time_str, "home": home, "opponent": opp}
    return games


def fetch_all(old, today):
    """old = last run's games. A team that HAD upcoming games and now shows none is an error
    (blocked / layout change). A team we've never seen with no games just isn't posted yet."""
    had_upcoming = {g["team"] for g in old.values() if g["date"] >= today}
    allgames = {}
    for team, url in TEAMS:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 404 and team not in had_upcoming:
            print(f"WARNING: {team}: page not found (404) - check the URL, or schedule not created yet")
            continue
        r.raise_for_status()
        g = parse_schedule(r.text, team)
        if not g:
            if team in had_upcoming:
                raise RuntimeError(f"Parsed 0 games for {team} - page layout changed or request was blocked")
            print(f"WARNING: {team}: no games posted yet")
            continue
        unknown = [c for c, v in g.items() if v["home"] is None]
        if unknown:
            raise RuntimeError(f"{team}: could not tell home/away for {len(unknown)} games - parser needs a look")
        allgames.update(g)
    return allgames


def fmt12(t):
    if not t:
        return "TBA"
    return datetime.strptime(t, "%H:%M").strftime("%-I:%M%p").lower()


def diff(old, new, today):
    msgs = []
    known_teams = {g["team"] for g in old.values()}
    for cid, n in new.items():
        o = old.get(cid)
        if n["date"] < today:
            continue
        if o is None:
            if n["team"] not in known_teams:
                continue  # whole team was just added - don't alert for every game
            if n["home"] or INCLUDE_AWAY:
                msgs.append(f"NEW: {n['team']} {'vs' if n['home'] else '@'} {n['opponent']} {n['date']} {fmt12(n['time'])}")
            continue
        watched = n["home"] or o["home"] or INCLUDE_AWAY
        if not watched:
            continue
        bits = []
        if o["date"] != n["date"]:
            bits.append(f"date {o['date']} -> {n['date']}")
        if o["time"] != n["time"]:
            bits.append(f"time {fmt12(o['time'])} -> {fmt12(n['time'])}")
        if o["home"] != n["home"]:
            bits.append("now HOME" if n["home"] else "now AWAY")
        if bits:
            msgs.append(f"CHANGED: {n['team']} vs {n['opponent']} ({n['date']}): " + ", ".join(bits))
    for cid, o in old.items():
        if cid not in new and o["date"] >= today and (o["home"] or INCLUDE_AWAY):
            msgs.append(f"REMOVED: {o['team']} vs {o['opponent']} {o['date']} {fmt12(o['time'])}")
    return msgs


def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(games):
    days = {}
    for g in games.values():
        if g["home"]:
            days.setdefault(g["date"], []).append(g)
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//maxpreps-concessions//EN",
           "CALSCALE:GREGORIAN", "X-WR-CALNAME:Concessions",
           "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for date, gs in sorted(days.items()):
        gs.sort(key=lambda g: g["time"] or "99:99")
        timed = [g for g in gs if g["time"]]
        parts = " / ".join(f"{g['team']} {fmt12(g['time'])}" for g in gs)
        opps = sorted({g["opponent"] for g in gs if g["opponent"] not in ("TBD", "JV Opponent")})
        summary = f"Concessions: {parts}" + (f" vs {', '.join(opps)}" if opps else "")
        desc = "\n".join(f"{g['team']}: {fmt12(g['time'])} vs {g['opponent']}" for g in gs)
        desc += f"\n\nEvent starts {LEAD_MINUTES} min before first game. Source: MaxPreps (auto-synced)."
        out += ["BEGIN:VEVENT", f"UID:concessions-{date}@maxpreps-sync", f"DTSTAMP:{now}"]
        if timed:
            first = datetime.strptime(f"{date} {timed[0]['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            last = datetime.strptime(f"{date} {timed[-1]['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            s = (first - timedelta(minutes=LEAD_MINUTES)).astimezone(timezone.utc)
            e = (last + timedelta(minutes=TAIL_MINUTES)).astimezone(timezone.utc)
            out += [f"DTSTART:{s:%Y%m%dT%H%M%SZ}", f"DTEND:{e:%Y%m%dT%H%M%SZ}"]
        else:  # time TBA -> all-day event
            d0 = datetime.strptime(date, "%Y-%m-%d")
            out += [f"DTSTART;VALUE=DATE:{d0:%Y%m%d}", f"DTEND;VALUE=DATE:{d0 + timedelta(days=1):%Y%m%d}"]
        out += [f"SUMMARY:{esc(summary)}", f"DESCRIPTION:{esc(desc)}", f"LOCATION:{esc(HOME_LOCATION)}",
                "END:VEVENT"]
    out.append("END:VCALENDAR")
    folded = []
    for line in out:  # RFC 5545: fold lines longer than 75 bytes
        while len(line.encode()) > 74:
            cut = 74
            while len(line[:cut].encode()) > 74:
                cut -= 1
            folded.append(line[:cut]); line = " " + line[cut:]
        folded.append(line)
    return "\r\n".join(folded) + "\r\n"


def notify(title, message):
    print(f"[notify] {title}\n{message}")
    url = os.environ.get("NOTIFY_WEBHOOK")
    if url:
        try:
            requests.post(url, json={"title": title, "message": message}, timeout=15)
        except Exception as ex:
            print(f"notify failed: {ex}", file=sys.stderr)


def main():
    dry = "--dry-run" in sys.argv
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    first_run = not STATE.exists()
    old = {} if first_run else json.loads(STATE.read_text())
    try:
        games = fetch_all(old, today)
        if not games:
            raise RuntimeError("No games found on any page")
    except Exception as ex:
        if not dry:
            notify("Concession schedule sync FAILED", str(ex))
        print(f"ERROR: {ex}", file=sys.stderr)
        sys.exit(1)
    if dry:
        for g in sorted(games.values(), key=lambda g: (g["date"], g["time"] or "")):
            print(g["date"], fmt12(g["time"]).rjust(7), "HOME" if g["home"] else "away", g["team"].ljust(12), g["opponent"])
        return
    changes = [] if first_run else diff(old, games, today)
    OUT_ICS.parent.mkdir(parents=True, exist_ok=True)
    OUT_ICS.write_text(build_ics(games), newline="")
    STATE.write_text(json.dumps(games, indent=1, sort_keys=True))
    if changes:
        notify("Concessions schedule changed", "\n".join(changes))
    else:
        print("No changes." if not first_run else f"First run: {len(games)} games recorded.")


if __name__ == "__main__":
    main()
