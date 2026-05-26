# migri-time

Monitors the [Finnish Immigration Service (Migri)](https://migri.vihta.com/public/migri/#/reservation) booking API for available **citizenship appointment** slots across all offices in Finland. Ranks them by a combined score of time + distance from Helsinki and notifies whenever any new slot appears.

---

## Files

| File | Description |
|---|---|
| `crawler.py` | Main script — all logic |
| `requirements.txt` | Python dependencies (`requests`) |
| `migri_appointments.log` | Append-only log of every check (created at runtime) |
| `slots.txt` | Overwritten each check — full ranked table of all current slots |
| `crawler.pid` | PID of the background process (created when started in background) |

---

## Setup (one-time)

```bash
cd /path/to/migri-time
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
brew install terminal-notifier   # enables clickable notifications
```

---

## Start / Stop

### Foreground (Ctrl+C to stop)
```bash
source .venv/bin/activate
python3 crawler.py
```

### Background (keeps running after closing the terminal)
```bash
source .venv/bin/activate
nohup python3 crawler.py >> migri_appointments.log 2>&1 &
echo $! > crawler.pid
```

### Stop the background process
```bash
kill $(cat crawler.pid)
```

### Check if it's still running
```bash
ps -p $(cat crawler.pid)
```

### Restart after a config change
```bash
kill $(cat crawler.pid)
nohup python3 crawler.py >> migri_appointments.log 2>&1 &
echo $! > crawler.pid
```

---

## View available slots

Open `slots.txt` for a clean ranked table of all currently available slots, updated every check:

```
#    Date & Time            Office                                   Dist (km)    Score
------------------------------------------------------------------------------------------
1    Wed 27 May 2026 08:15  Lappeenrannan palvelupiste               222          1.96
2    Wed 27 May 2026 12:50  Vaasan palvelupiste                      418          3.13
...
```

Or watch the live log:
```bash
tail -f migri_appointments.log
```

---

## Notifications

When new slots appear, a macOS notification is sent showing the top 2 ranked slots. If `terminal-notifier` is installed, **clicking the notification opens `slots.txt`** directly.

If notifications don't appear:
1. Go to **System Settings → Privacy & Security → Notifications**
2. Find **terminal-notifier** (or Terminal/iTerm2) and enable notifications
3. Restart the crawler

---

## Configuration

All options are constants at the top of `crawler.py`:

| Constant | Default | Description |
|---|---|---|
| `CHECK_INTERVAL_SECONDS` | `3600` | How often to poll (seconds). 3600 = every hour. |
| `DISTANCE_WEIGHT` | `200` | How many km equals 1 extra day in the score. Increase to prioritise time more; decrease to prioritise proximity more. |
| `LOG_FILE` | `migri_appointments.log` | Append-only log file path. |
| `SLOTS_FILE` | `slots.txt` | Current slots table, overwritten each check. |
| `NOTIFICATION_SOUND` | `Glass` | macOS system sound name for alerts. |

---

## How slots are ranked

`score = days_until_appointment + distance_km / DISTANCE_WEIGHT`

Lower score = better. With the default weight of 200, travelling 200 km counts the same as waiting 1 extra day. Adjust `DISTANCE_WEIGHT` to shift the balance.

---

## Log format

```
[2026-05-26T11:57:26] INFO  | STARTUP  | Migri citizenship crawler started. Interval: 3600s | Distance weight: 200 km/day
[2026-05-26T11:57:26] INFO  | SESSION  | Token acquired (4e1ebd24...)
[2026-05-26T11:57:27] INFO  | CHECK    | #1   Lappeenrannan palvelupiste    2026-05-27 08:15  score=1.96  ~222 km
[2026-05-26T11:57:27] INFO  | CHECK    | #2   Vaasan palvelupiste           2026-05-27 12:50  score=3.13  ~418 km
[2026-05-26T11:57:27] INFO  | CHECK    | 12 new slot(s) of 12 total → notification sent
[2026-05-26T12:57:27] INFO  | CHECK    | #1   Lappeenrannan palvelupiste    2026-05-27 08:15  score=1.95  ~222 km
[2026-05-26T12:57:27] INFO  | CHECK    | no new slots (12 total, unchanged)
[2026-05-26T12:57:27] WARN  | SESSION  | Token expired, re-authenticating
```

---

## Troubleshooting

### "Could not create session" / site under maintenance
The Migri booking system occasionally goes into maintenance. The crawler retries automatically every hour. Check https://migri.vihta.com/public/migri/ in a browser to confirm.

### "No appointments found" every check
Either no slots are genuinely available, or the API shape changed. Run this to check raw API output:
```bash
source .venv/bin/activate
python3 -c "
import requests
s = requests.Session()
h = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json', 'Referer': 'https://migri.vihta.com/public/migri/'}
sid = s.get('https://migri.vihta.com/public/migri/api/sessions?language=en', headers=h).json()['id']
print(s.get('https://migri.vihta.com/public/migri/api/upcoming/services/000564ce-b800-4c2e-8040-62f50a09f55e', headers={**h, 'vihta-session': sid}).json())
"
```
