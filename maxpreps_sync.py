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
import json, os, re, sys, time
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
FAILS = Path("failures.json")   # consecutive failed runs; alert only after ALERT_AFTER of them
ALERT_AFTER = 3
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
    links = soup.find_all("a", href=MATCH_RE)
    # MaxPreps adds "Last Match" / "Next Match" boxes above the table that link to the same games
    # without a vs/@ - only read links that sit inside a schedule table row
    in_rows = [a for a in links if a.find_parent("tr")]
    for a in (in_rows or links):
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


def get_with_retry(url, attempts=3, wait=30):
    """MaxPreps sometimes answers 403 'Geo-block' to a GitHub runner in an unlucky region.
    Retry a few times before calling it a failure."""
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code in (403, 429) or r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code} {r.reason} for {url}")
            else:
                return r
        except requests.RequestException as ex:
            last = ex
        if i < attempts - 1:
            print(f"attempt {i + 1} failed ({last}); retrying in {wait}s")
            time.sleep(wait)
    raise last


def fetch_all(old, today):
    """old = last run's games. A team that HAD upcoming games and now shows none is an error
    (blocked / layout change). A team we've never seen with no games just isn't posted yet."""
    had_upcoming = {g["team"] for g in old.values() if g["date"] >= today}
    allgames = {}
    for team, url in TEAMS:
        r = get_with_retry(url)
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
        for c, v in g.items():
            if v["home"] is None:
                print(f"WARNING: {team}: home/away unknown for {v['date']} vs {v['opponent']} - shown on calendar flagged")
                v["opponent"] += " (CHECK MAXPREPS - home/away unknown)"
        allgames.update(g)
    return allgames


def fmt12(t):
    if not t:
        return "TBA"
    return datetime.strptime(t, "%H:%M").strftime("%-I:%M%p").lower()


# Replacement for the diff() function in maxpreps_sync.py
# (rsmart22/fca-concessions-calendar)
#
# Change: same-day time changes to/from TBA are ignored. MaxPreps blanks a
# game's time while it is being played and restores it afterward, which used
# to fire two useless "schedule changed" pushes on every game day.
# Everything else (new games, moved times, future-game TBAs, removals,
# home/away flips) alerts exactly as before.

def diff_records(old, new, today):
    """Structured list of schedule changes between two runs. Each record has:
    type (NEW/CHANGED/REMOVED), team, date, opponent, home, old_time, new_time, old_date, new_date, note.
    Away-only changes are ignored unless INCLUDE_AWAY."""
    recs = []
    known_teams = {g["team"] for g in old.values()}
    ha = lambda g: "HOME" if g["home"] else ("?" if g["home"] is None else "AWAY")
    for cid, n in new.items():
        o = old.get(cid)
        if n["date"] < today:
            continue
        if o is None:
            if n["team"] not in known_teams:
                continue  # whole team was just added - don't alert for every game
            if n["home"] or n["home"] is None or INCLUDE_AWAY:
                recs.append(dict(type="NEW", team=n["team"], date=n["date"], opponent=n["opponent"], home=ha(n),
                                 old_time="", new_time=fmt12(n["time"]), old_date="", new_date=n["date"], note="game added"))
            continue
        watched = n["home"] or o["home"] or n["home"] is None or o["home"] is None or INCLUDE_AWAY
        if not watched:
            continue
        bits = []
        if o["date"] != n["date"]:
            bits.append(f"date {o['date']} -> {n['date']}")
        if o["time"] != n["time"]:
            # MaxPreps blanks the time to TBA while a game is live and puts it
            # back when it ends. A same-day change to/from TBA is that flap,
            # not a schedule change - we're already at the game. Skip it.
            live_flap = n["date"] == today and (o["time"] is None or n["time"] is None)
            if not live_flap:
                bits.append(f"time {fmt12(o['time'])} -> {fmt12(n['time'])}")
        if o["home"] != n["home"]:
            bits.append("now HOME" if n["home"] else "now AWAY")
        if bits:
            recs.append(dict(type="CHANGED", team=n["team"], date=n["date"], opponent=n["opponent"], home=ha(n),
                             old_time=fmt12(o["time"]), new_time=fmt12(n["time"]), old_date=o["date"], new_date=n["date"],
                             note=", ".join(bits)))
    for cid, o in old.items():
        if cid not in new and o["date"] >= today and (o["home"] or INCLUDE_AWAY):
            recs.append(dict(type="REMOVED", team=o["team"], date=o["date"], opponent=o["opponent"], home=ha(o),
                             old_time=fmt12(o["time"]), new_time="", old_date=o["date"], new_date="", note="game removed"))
    return recs


def diff(old, new, today):
    """Human-readable change lines (what the phone alert says)."""
    msgs = []
    for r in diff_records(old, new, today):
        if r["type"] == "NEW":
            msgs.append(f"NEW: {r['team']} {'@' if r['home'] == 'AWAY' else 'vs'} {r['opponent']} {r['date']} {r['new_time']}")
        elif r["type"] == "CHANGED":
            msgs.append(f"CHANGED: {r['team']} vs {r['opponent']} ({r['date']}): {r['note']}")
        else:
            msgs.append(f"REMOVED: {r['team']} vs {r['opponent']} {r['date']} {r['old_time']}")
    return msgs


# ----------------------------- change log -----------------------------
LOG_CSV = Path("docs/changes.csv")
LOG_HTML = Path("docs/changes.html")
LOG_COLS = ["detected", "type", "team", "game_date", "opponent", "home_away", "old_time", "new_time",
            "old_date", "new_date", "detail"]


def append_change_log(recs, when=None):
    """Append change records to docs/changes.csv (one row each) and rebuild docs/changes.html."""
    import csv
    when = when or datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    new_file = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(LOG_COLS)
        for r in recs:
            w.writerow([when, r["type"], r["team"], r["date"], r["opponent"], r["home"], r["old_time"],
                        r["new_time"], r["old_date"], r["new_date"], r["note"]])
    build_change_page()


def build_change_page():
    import csv, html
    rows = []
    if LOG_CSV.exists():
        with LOG_CSV.open(newline="") as f:
            rows = list(csv.DictReader(f))
    rows.reverse()  # newest first
    def day(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%a %-m/%-d/%y")
        except Exception:
            return d
    body = []
    for r in rows:
        cls = {"NEW": "new", "CHANGED": "chg", "REMOVED": "rem"}.get(r["type"], "")
        if r["type"] == "CHANGED":
            what = r["detail"]
        elif r["type"] == "NEW":
            what = f"added, {r['new_time']}"
        else:
            what = f"removed (was {r['old_time']})"
        body.append(f"<tr class='{cls}'><td>{html.escape(r['detected'])}</td><td><b>{r['type']}</b></td>"
                    f"<td>{html.escape(day(r['game_date']))}</td><td>{html.escape(r['team'])}</td>"
                    f"<td>{html.escape(r['home_away'])} {html.escape(r['opponent'])}</td><td>{html.escape(what)}</td></tr>")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Concessions schedule changes</title>
<style>body{{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:16px;background:#fff;color:#111}}
h1{{font-size:1.3em}} p{{color:#555;font-size:.9em}} table{{border-collapse:collapse;width:100%;font-size:.9em}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #ddd;vertical-align:top}} th{{background:#f3f3f3;position:sticky;top:0}}
tr.new td:nth-child(2){{color:#1a7f37}} tr.chg td:nth-child(2){{color:#9a6700}} tr.rem td:nth-child(2){{color:#cf222e}}
@media(max-width:600px){{table{{font-size:.8em}} td,th{{padding:5px 4px}}}}</style></head><body>
<h1>Concessions schedule changes</h1>
<p>Every change MaxPreps made to a First Christian home game, as detected by the hourly sync. Newest first.
Page rebuilt {datetime.now(TZ).strftime('%b %-d, %Y %-I:%M %p')} ET. <a href="changes.csv">Download as spreadsheet (CSV)</a>.</p>
<table><thead><tr><th>Detected</th><th>Change</th><th>Game date</th><th>Team</th><th>Opponent</th><th>What changed</th></tr></thead>
<tbody>{''.join(body) if body else '<tr><td colspan=6>No changes recorded yet.</td></tr>'}</tbody></table></body></html>"""
    LOG_HTML.write_text(page)


def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(games):
    days = {}
    for g in games.values():
        if g["home"] or g["home"] is None:
            days.setdefault(g["date"], []).append(g)
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//maxpreps-concessions//EN",
           "CALSCALE:GREGORIAN", "X-WR-CALNAME:Concessions",
           "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for date, gs in sorted(days.items()):
        gs.sort(key=lambda g: g["time"] or "99:99")
        timed = [g for g in gs if g["time"]]
        # Title leads with "how big is this day": e.g. "BB 4 of 4 teams - 1st game 4:30pm - NCA"
        # Sport = first word of the team label; "of N" = how many teams of that sport are in TEAMS.
        sports = []
        for g in gs:
            sp = g["team"].split()[0]
            if sp not in sports:
                sports.append(sp)
        counts = []
        for sp in sports:
            playing = len({g["team"] for g in gs if g["team"].split()[0] == sp})
            total = sum(1 for label, _ in TEAMS if label.split()[0] == sp)
            counts.append(f"{sp} {playing} of {max(total, playing)} teams")
        opps = sorted({g["opponent"] for g in gs
                       if g["opponent"] != "TBD" and not g["opponent"].endswith(" Opponent")})
        first = fmt12(timed[0]["time"]) if timed else "TBA"
        summary = " + ".join(counts) + f" - 1st game {first}" + (f" - {'/'.join(opps)}" if opps else "")
        desc = " + ".join(counts) + " playing\n\n" + "\n".join(f"{g['team']}: {fmt12(g['time'])} vs {g['opponent']}" for g in gs)
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
        print(f"ERROR: {ex}", file=sys.stderr)
        if not dry:
            n = (json.loads(FAILS.read_text())["count"] if FAILS.exists() else 0) + 1
            FAILS.write_text(json.dumps({"count": n, "last_error": str(ex)}))
            if n == ALERT_AFTER or (n > ALERT_AFTER and n % 24 == 0):
                notify("Concession schedule sync FAILED",
                       f"{n} hourly checks in a row have failed. Calendar is frozen at the last good "
                       f"schedule - check MaxPreps by hand.\nLast error: {ex}")
        sys.exit(1)
    if dry:
        for g in sorted(games.values(), key=lambda g: (g["date"], g["time"] or "")):
            print(g["date"], fmt12(g["time"]).rjust(7), "HOME" if g["home"] else "away", g["team"].ljust(12), g["opponent"])
        return
    recs = [] if first_run else diff_records(old, games, today)
    changes = diff(old, games, today) if recs else []
    OUT_ICS.parent.mkdir(parents=True, exist_ok=True)
    OUT_ICS.write_text(build_ics(games), newline="")
    STATE.write_text(json.dumps(games, indent=1, sort_keys=True))
    if FAILS.exists():
        n = json.loads(FAILS.read_text())["count"]
        FAILS.unlink()
        if n >= ALERT_AFTER:
            notify("Concession schedule sync recovered", f"Back to normal after {n} failed checks.")
    if recs:
        append_change_log(recs)
    elif not LOG_HTML.exists():
        build_change_page()
    if changes:
        notify("Concessions schedule changed", "\n".join(changes))
    else:
        print("No changes." if not first_run else f"First run: {len(games)} games recorded.")


if __name__ == "__main__":
    main()
