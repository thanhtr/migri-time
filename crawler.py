#!/usr/bin/env python3
"""
Migri citizenship appointment crawler.
Two data sources per check:
  1. upcoming/services API  — all offices, ~12 next available slots
  2. weekly scheduling API  — top 5 closest offices, next 2 weeks

Notifies when any new slot appears:
  Line 1: globally earliest slot from source 1 (by datetime)
  Line 2: earliest slot from source 2 (top-5 closest weekly scan)
"""

import datetime
import os
import subprocess
import sys
import time

import requests

# ── Configuration ─────────────────────────────────────────────────────────────
CHECK_INTERVAL_SECONDS = 60 * 60   # how often to poll (default: every hour)
MAX_RETRIES            = 3
RETRY_BASE_DELAY       = 2         # seconds; delay = BASE ** attempt
RETRY_MAX_DELAY        = 60
LOG_FILE               = "migri_appointments.log"
SLOTS_FILE             = "slots.txt"

# Scoring: lower = better.  score = days_until + dist_km / DISTANCE_WEIGHT
DISTANCE_WEIGHT = 200   # 200 km ≈ 1 extra day

CITY_DISTANCES_KM: dict[str, int] = {
    "helsinki":       5,
    "lahti":        103,
    "tampere":      176,
    "turku":        167,
    "raisio":       167,
    "lappeenranta": 222,
    "mikkeli":      232,
    "pori":         246,
    "jyväskylä":    271,
    "joensuu":      441,
    "kuopio":       381,
    "vaasa":        418,
    "mariehamn":    310,
    "oulu":         607,
    "rovaniemi":    832,
}
DEFAULT_DISTANCE_KM = 500

SESSION_URL    = "https://migri.vihta.com/public/migri/api/sessions"
UPCOMING_URL   = "https://migri.vihta.com/public/migri/api/upcoming/services/{service_id}"
OFFICES_URL    = "https://migri.vihta.com/public/migri/api/offices"
SCHEDULING_URL = (
    "https://migri.vihta.com/public/migri/api/scheduling/offices"
    "/{office_id}/{year}/w{week}"
)
SERVICE_ID     = "000564ce-b800-4c2e-8040-62f50a09f55e"   # Kansalaisuusasiat

WEEKLY_SCAN_WEEKS = 2
WEEKLY_TOP_N      = 5
SCHEDULING_BODY   = {"serviceSelections": [{"values": [SERVICE_ID]}], "extraServices": []}
SCHEDULING_PARAMS = {"start_hours": 0, "end_hours": 23, "max_amount": 24}

NOTIFICATION_TITLE = "Migri Citizenship Appointment"
NOTIFICATION_SOUND = "Glass"

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://migri.vihta.com/public/migri/",
    "Accept-Language": "en-US,en;q=0.9,fi;q=0.8",
}
# ──────────────────────────────────────────────────────────────────────────────


class SessionExpiredError(Exception):
    pass


# ── Logging ───────────────────────────────────────────────────────────────────

def log(level: str, component: str, message: str) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{timestamp}] {level:<5} | {component:<8} | {message}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[LOG ERROR] Could not write to {LOG_FILE}: {e}", flush=True)


# ── macOS notification ────────────────────────────────────────────────────────

def _terminal_notifier_available() -> bool:
    try:
        result = subprocess.run(
            ["terminal-notifier", "-help"],
            capture_output=True, timeout=3,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def notify(title: str, message: str, open_file: "str | None" = None) -> None:
    """
    Send a macOS notification.
    If terminal-notifier is installed and open_file is given, clicking opens that file.
    Falls back to osascript otherwise.
    """
    if open_file and _terminal_notifier_available():
        file_url = f"file://{os.path.abspath(open_file)}"
        try:
            subprocess.run(
                [
                    "terminal-notifier",
                    "-title",   title,
                    "-message", message,
                    "-sound",   NOTIFICATION_SOUND,
                    "-open",    file_url,
                ],
                timeout=5, check=False,
            )
            return
        except Exception as e:
            log("WARN", "NOTIFY", f"terminal-notifier failed: {e}")

    safe_msg   = message.replace("\\", "\\\\").replace('"', '\\"')
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'display notification "{safe_msg}" '
        f'with title "{safe_title}" '
        f'sound name "{NOTIFICATION_SOUND}"'
    )
    try:
        subprocess.run(["osascript", "-e", script], timeout=5, check=False)
    except Exception as e:
        log("WARN", "NOTIFY", f"osascript failed: {e}")


# ── Session ───────────────────────────────────────────────────────────────────

def create_session(http: requests.Session) -> str:
    resp = http.get(
        SESSION_URL,
        headers=BASE_HEADERS,
        params={"language": "en"},
        timeout=10,
    )
    if resp.status_code in (401, 403):
        raise SessionExpiredError()
    resp.raise_for_status()
    session_id = resp.json().get("id")
    if not session_id:
        raise RuntimeError(f"No session ID in response. Body: {resp.text[:200]}")
    return session_id


def auth_headers(session_id: str) -> dict:
    return {**BASE_HEADERS, "vihta-session": session_id}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def fetch_with_retry(
    http: requests.Session,
    url: str,
    headers: dict,
) -> "requests.Response | None":
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = http.get(url, headers=headers, timeout=10)
            if resp.status_code in (401, 403):
                raise SessionExpiredError()
            return resp
        except SessionExpiredError:
            raise
        except Exception as e:
            delay = min(RETRY_BASE_DELAY ** attempt, RETRY_MAX_DELAY)
            log("WARN", "NETWORK", f"GET attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(delay)
    log("ERROR", "NETWORK", f"All GET retries exhausted for {url}")
    return None


def post_with_retry(
    http: requests.Session,
    url: str,
    headers: dict,
    json_body: dict,
    params: "dict | None" = None,
) -> "requests.Response | None":
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = http.post(url, headers=headers, json=json_body, params=params, timeout=10)
            if resp.status_code in (401, 403):
                raise SessionExpiredError()
            return resp
        except SessionExpiredError:
            raise
        except Exception as e:
            delay = min(RETRY_BASE_DELAY ** attempt, RETRY_MAX_DELAY)
            log("WARN", "NETWORK", f"POST attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(delay)
    log("ERROR", "NETWORK", f"All POST retries exhausted for {url}")
    return None


# ── Slot parsing & ranking (upcoming API) ─────────────────────────────────────

def parse_timestamp(raw: str) -> "datetime.datetime | None":
    try:
        normalized = raw.replace("Z", "+00:00")
        dt_utc = datetime.datetime.fromisoformat(normalized)
        return dt_utc.astimezone(tz=None).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def slot_score(dt: datetime.datetime, city: str) -> float:
    days = (dt - datetime.datetime.now()).total_seconds() / 86400
    dist = CITY_DISTANCES_KM.get(city.lower(), DEFAULT_DISTANCE_KM)
    return days + dist / DISTANCE_WEIGHT


def rank_slots(data: dict) -> list[tuple[float, datetime.datetime, str, int]]:
    """
    Parse upcoming/services response.
    Returns (score, local_dt, office_name, dist_km) sorted best-first.
    """
    availabilities = data.get("availabilities") or []
    offices        = data.get("offices") or []
    results: list[tuple[float, datetime.datetime, str, int]] = []

    for slot in availabilities:
        dt = parse_timestamp(slot.get("startTimestamp", ""))
        if dt is None:
            continue
        office_idx = slot.get("office")
        if isinstance(office_idx, int) and office_idx < len(offices):
            o    = offices[office_idx]
            city = o.get("address", {}).get("city", "")
            name = o.get("name", "") or city
        else:
            city = ""
            name = f"office#{office_idx}"
        dist  = CITY_DISTANCES_KM.get(city.lower(), DEFAULT_DISTANCE_KM)
        score = slot_score(dt, city)
        results.append((score, dt, name, dist))

    return sorted(results)


# ── Offices & weekly scan ─────────────────────────────────────────────────────

def fetch_offices(
    http: requests.Session,
    session_id: str,
) -> list[tuple[int, str, str]]:
    """
    GET /api/offices → (dist_km, office_id, office_name) sorted by distance.
    """
    resp = fetch_with_retry(http, OFFICES_URL, auth_headers(session_id))
    if resp is None or resp.status_code != 200:
        log("WARN", "API", "Could not fetch offices list")
        return []
    try:
        offices = resp.json().get("offices") or []
    except Exception:
        log("WARN", "API", "Invalid JSON from offices endpoint")
        return []

    results = []
    for o in offices:
        oid  = o.get("id", "")
        city = o.get("address", {}).get("city", "")
        name = o.get("name", "") or city
        dist = CITY_DISTANCES_KM.get(city.lower(), DEFAULT_DISTANCE_KM)
        results.append((dist, oid, name))

    return sorted(results)


def parse_weekly_slots(
    daily_times: list,
    year: int,
    week: int,
    day_index: int,
) -> list[datetime.datetime]:
    """
    Extract datetimes from one day's slot list in a dailyTimesByOffice response.
    day_index: 0=Monday … 6=Sunday
    Tries multiple field names; handles bare ISO strings and time-only strings.
    """
    date = datetime.date.fromisocalendar(year, week, day_index + 1)
    now  = datetime.datetime.now()
    results = []

    for entry in (daily_times or []):
        raw = None
        if isinstance(entry, str):
            raw = entry
        elif isinstance(entry, dict):
            for field in ("startTime", "start", "time", "dateTime"):
                if field in entry:
                    raw = entry[field]
                    break

        if raw is None:
            continue

        dt = parse_timestamp(raw)

        if dt is None:
            # Try time-only string e.g. "09:15:00" or "09:15"
            try:
                t  = datetime.time.fromisoformat(str(raw)[:8].strip())
                dt = datetime.datetime.combine(date, t)
            except (ValueError, TypeError):
                pass

        if dt is not None and dt > now:
            results.append(dt)

    return results


def scan_weekly_closest(
    http: requests.Session,
    session_id: str,
) -> "tuple[datetime.datetime, str, int] | tuple[None, None, None]":
    """
    Scan weekly scheduling endpoint for next WEEKLY_SCAN_WEEKS weeks
    across the WEEKLY_TOP_N closest offices.
    Returns (earliest_dt, office_name, dist_km) or (None, None, None).
    """
    offices = fetch_offices(http, session_id)
    if not offices:
        return None, None, None

    top_offices = offices[:WEEKLY_TOP_N]
    today   = datetime.date.today()
    hdrs    = {**auth_headers(session_id), "Content-Type": "application/json"}

    weeks_to_scan = []
    for delta in range(WEEKLY_SCAN_WEEKS):
        d   = today + datetime.timedelta(weeks=delta)
        iso = d.isocalendar()
        weeks_to_scan.append((iso.year, iso.week))

    earliest_dt:     datetime.datetime | None = None
    earliest_office: str | None               = None
    earliest_dist:   int | None               = None

    for dist, oid, name in top_offices:
        for year, week in weeks_to_scan:
            start_date = datetime.date.fromisocalendar(year, week, 1).isoformat()
            params     = {**SCHEDULING_PARAMS, "start_date": start_date}
            url        = SCHEDULING_URL.format(office_id=oid, year=year, week=week)

            resp = post_with_retry(http, url, hdrs, SCHEDULING_BODY, params=params)

            if resp is None or resp.status_code != 200:
                status = resp.status_code if resp else "None"
                log("WARN", "WEEKLY", f"No response for {name} w{week}/{year} (status {status})")
                continue

            try:
                data = resp.json()
            except Exception:
                log("WARN", "WEEKLY", f"Invalid JSON for {name} w{week}/{year}")
                continue

            daily = data.get("dailyTimesByOffice") or []
            for day_idx, day_slots in enumerate(daily[:7]):
                for dt in parse_weekly_slots(day_slots, year, week, day_idx):
                    if earliest_dt is None or dt < earliest_dt:
                        earliest_dt     = dt
                        earliest_office = name
                        earliest_dist   = dist

    return earliest_dt, earliest_office, earliest_dist


# ── Upcoming API check ────────────────────────────────────────────────────────

def check_upcoming(
    http: requests.Session,
    session_id: str,
) -> list[tuple[float, datetime.datetime, str, int]]:
    """Fetch and rank upcoming slots. Returns [] on error."""
    url  = UPCOMING_URL.format(service_id=SERVICE_ID)
    resp = fetch_with_retry(http, url, auth_headers(session_id))

    if resp is None:
        return []
    if resp.status_code != 200:
        log("WARN", "API", f"Unexpected status {resp.status_code}")
        return []
    try:
        data = resp.json()
    except Exception:
        log("WARN", "API", "Invalid JSON in response")
        return []

    return rank_slots(data)


# ── Slots file ────────────────────────────────────────────────────────────────

def write_slots_file(
    ranked: list[tuple[float, datetime.datetime, str, int]],
    weekly_result: "tuple[datetime.datetime | None, str | None, int | None]",
) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    w_dt, w_office, w_dist = weekly_result

    lines = [
        f"Migri citizenship appointments — last checked {now}",
        f"Score = days_until + dist_km / {DISTANCE_WEIGHT}  (lower is better)",
        "",
        "=== Upcoming (all offices) ===",
        f"{'#':<4} {'Date & Time':<22} {'Office':<40} {'Dist (km)':<12} Score",
        "-" * 90,
    ]
    for i, (score, dt, office, dist) in enumerate(ranked, 1):
        lines.append(
            f"{i:<4} {dt.strftime('%a %d %b %Y %H:%M'):<22} {office:<40} {dist:<12} {score:.2f}"
        )
    if not ranked:
        lines.append("  (no appointments available)")

    lines += [
        "",
        f"=== Next {WEEKLY_SCAN_WEEKS} weeks — top {WEEKLY_TOP_N} closest offices ===",
        f"{'#':<4} {'Date & Time':<22} {'Office':<40} {'Dist (km)'}",
        "-" * 80,
    ]
    if w_dt and w_office and w_dist is not None:
        lines.append(
            f"{'1':<4} {w_dt.strftime('%a %d %b %Y %H:%M'):<22} {w_office:<40} {w_dist}"
        )
    else:
        lines.append("  (no appointments found)")

    lines.append("")
    try:
        with open(SLOTS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except OSError as e:
        log("WARN", "FILE", f"Could not write {SLOTS_FILE}: {e}")


# ── Formatting ────────────────────────────────────────────────────────────────

def format_slot(dt: datetime.datetime) -> str:
    return dt.strftime("%a %d %b %Y %H:%M")


def slot_key(dt: datetime.datetime, office: str) -> str:
    return f"{dt.isoformat()}|{office}"


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    log("INFO", "STARTUP", (
        f"Migri citizenship crawler started. "
        f"Interval: {CHECK_INTERVAL_SECONDS}s | "
        f"Distance weight: {DISTANCE_WEIGHT} km/day | "
        f"Weekly scan: {WEEKLY_TOP_N} closest offices × {WEEKLY_SCAN_WEEKS} weeks"
    ))

    http: requests.Session       = requests.Session()
    session_id: str | None       = None
    previous_slot_keys: set[str] = set()

    while True:
        # ① Ensure valid session
        if session_id is None:
            try:
                session_id = create_session(http)
                log("INFO", "SESSION", f"Token acquired ({session_id[:8]}...)")
            except Exception as e:
                log("ERROR", "SESSION", f"Could not create session: {e}")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

        # ② Fetch upcoming slots (all offices)
        try:
            ranked = check_upcoming(http, session_id)
        except SessionExpiredError:
            log("WARN", "SESSION", "Token expired, re-authenticating")
            session_id = None
            continue
        except Exception as e:
            log("ERROR", "SCAN", f"Unexpected error in upcoming scan: {e}")
            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        # ③ Weekly scan — top 5 closest offices
        try:
            w_dt, w_office, w_dist = scan_weekly_closest(http, session_id)
        except SessionExpiredError:
            log("WARN", "SESSION", "Token expired during weekly scan, re-authenticating")
            session_id = None
            continue
        except Exception as e:
            log("ERROR", "WEEKLY", f"Unexpected error in weekly scan: {e}")
            w_dt, w_office, w_dist = None, None, None

        # ④ Detect new slots (merged from both sources)
        current_slot_keys: set[str] = {slot_key(dt, o) for _, dt, o, _ in ranked}
        if w_dt is not None and w_office is not None:
            current_slot_keys.add(slot_key(w_dt, w_office))
        new_keys = current_slot_keys - previous_slot_keys

        # ⑤ Write slots file
        write_slots_file(ranked, (w_dt, w_office, w_dist))

        # ⑥ Log all results
        if not ranked:
            log("INFO", "CHECK", "No upcoming appointments found")
        else:
            for i, (score, dt, office, dist) in enumerate(ranked):
                log("INFO", "CHECK",
                    f"#{i+1:<3} {office:<40} {dt.strftime('%Y-%m-%d %H:%M')}  "
                    f"score={score:.2f}  ~{dist} km")

        if w_dt and w_office:
            log("INFO", "WEEKLY",
                f"Earliest: {w_office:<40} {w_dt.strftime('%Y-%m-%d %H:%M')}  ~{w_dist} km")
        else:
            log("INFO", "WEEKLY", "No appointments found in top-5 offices (next 2 weeks)")

        if new_keys:
            log("INFO", "CHECK", f"{len(new_keys)} new slot(s) → notification sent")
        else:
            log("INFO", "CHECK", f"no new slots ({len(current_slot_keys)} total, unchanged)")

        # ⑦ Notify if any new slots
        if new_keys:
            # Line 1: earliest by datetime from upcoming API
            if ranked:
                _, u_dt, u_office, u_dist = min(ranked, key=lambda x: x[1])
                line1 = f"1. {u_office} — {format_slot(u_dt)} (~{u_dist} km)"
            else:
                line1 = "1. none available"

            # Line 2: earliest from weekly closest scan
            if w_dt and w_office:
                line2 = f"2. {w_office} — {format_slot(w_dt)} (~{w_dist} km)"
            else:
                line2 = "2. none available"

            notify(NOTIFICATION_TITLE, f"{line1}\n{line2}", open_file=SLOTS_FILE)

        # ⑧ Update state and wait
        previous_slot_keys = current_slot_keys
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("INFO", "STARTUP", "Crawler stopped by user")
        sys.exit(0)
