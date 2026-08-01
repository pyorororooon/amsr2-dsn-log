#!/usr/bin/env python3
"""Poll DSN Now around AMSR2 Goldstone crossings.

Usage:
    python poll_dsn.py D       # descending pass of the day
    python poll_dsn.py A       # ascending pass of the day
    python poll_dsn.py once    # single poll, no waiting (for testing)

Reads dsn/goldstone_pass_schedule.json to decide which pass occurs today
and at exactly what time, then polls a tight window around it.
"""
import os
import sys
import csv
import json
import time
import pathlib
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta, date

URL = "https://eyes.nasa.gov/dsn/data/dsn.xml"
UA = os.environ.get("DSN_UA", "amsr2-rfi-research (contact: you@example.com)")

SCHEDULE = pathlib.Path("dsn/goldstone_pass_schedule.json")
RAW_DIR = pathlib.Path("dsn_archive/raw")
SUM_FILE = pathlib.Path("dsn_archive/summary.csv")

# Window around the crossing instant, and sampling interval.
BEFORE_S = int(os.environ.get("WINDOW_BEFORE_SEC", "600"))   # T-10 min
AFTER_S = int(os.environ.get("WINDOW_AFTER_SEC", "600"))     # T+10 min
INTERVAL = int(os.environ.get("POLL_INTERVAL_SEC", "60"))

# Passes to skip: Goldstone sits at the swath edge, far from nadir.
EXCLUDE = set(x for x in os.environ.get("EXCLUDE_PASSES", "034D,047D").split(",") if x)

FIELDS = [
    "poll_utc", "feed_utc", "pass_id", "node", "target_utc", "dt_sec",
    "station", "dish", "activity", "azimuth_deg", "elevation_deg", "wind_kmh",
    "up_active_bands", "up_x_power_kw", "up_spacecraft", "down_active_bands",
]


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------
def todays_pass(node, today=None):
    """Return (pass_id, target_datetime_utc) for `node` today, or None."""
    sched = json.loads(SCHEDULE.read_text(encoding="utf-8"))
    today = today or datetime.now(timezone.utc).date()
    epoch = date.fromisoformat(sched["cycle_epoch_utc"])
    anchor = date.fromisoformat(sched["anchor_date_utc"])
    doc = (today - epoch).days % sched["cycle_days"]

    for p in sched["passes"]:
        if p["node"] != node or p["doc"] != doc:
            continue
        if p["pass_id"] in EXCLUDE:
            print(f"pass {p['pass_id']} is excluded; nothing to do today")
            return None
        # linear drift correction from the anchor date
        sec = p["ref_sec"] + p["drift_sec_per_day"] * (today - anchor).days
        target = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) \
            + timedelta(seconds=sec)
        return p["pass_id"], target

    print(f"no {node} pass over Goldstone today (cycle day {doc})")
    return None


# --------------------------------------------------------------------------
# polling
# --------------------------------------------------------------------------
def fetch():
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.read()


def summarise(xml_bytes, poll_utc, ctx):
    """One row per dish."""
    root = ET.fromstring(xml_bytes)
    feed_ms = root.findtext("timestamp") or ""
    feed_utc = (
        datetime.fromtimestamp(int(feed_ms) / 1000, timezone.utc).isoformat()
        if feed_ms.isdigit() else ""
    )

    rows, station = [], ""
    for el in root:                       # <station> and <dish> are siblings
        if el.tag == "station":
            station = el.get("friendlyName", el.get("name", ""))
        elif el.tag == "dish":
            ups = [s for s in el.findall("upSignal") if s.get("active") == "true"]
            downs = [s for s in el.findall("downSignal") if s.get("active") == "true"]
            x_up = [s for s in ups if s.get("band") == "X"]
            row = dict(ctx)
            row.update({
                "poll_utc": poll_utc,
                "feed_utc": feed_utc,
                "station": station,
                "dish": el.get("name", ""),
                "activity": el.get("activity", ""),
                "azimuth_deg": el.get("azimuthAngle", ""),
                "elevation_deg": el.get("elevationAngle", ""),
                "wind_kmh": el.get("windSpeed", ""),
                "up_active_bands": ";".join(s.get("band", "") for s in ups),
                "up_x_power_kw": ";".join(s.get("power", "") for s in x_up),
                "up_spacecraft": ";".join(s.get("spacecraft", "") for s in ups),
                "down_active_bands": ";".join(s.get("band", "") for s in downs),
            })
            rows.append(row)
    return rows


def append_rows(rows):
    SUM_FILE.parent.mkdir(parents=True, exist_ok=True)
    new = not SUM_FILE.exists()
    with SUM_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def poll_once(ctx=None, target=None):
    now = datetime.now(timezone.utc)
    poll_utc = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx = dict(ctx or {"pass_id": "", "node": "", "target_utc": ""})
    ctx["dt_sec"] = round((now - target).total_seconds()) if target else ""

    try:
        data = fetch()
    except Exception as exc:
        print(f"{poll_utc} FETCH FAILED: {exc}", flush=True)
        return

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = poll_utc.replace(":", "").replace("-", "")
    tag = ctx.get("pass_id") or "adhoc"
    (RAW_DIR / f"dsn_{stamp}_{tag}.xml").write_bytes(data)

    try:
        rows = summarise(data, poll_utc, ctx)
        append_rows(rows)
        gds = [r for r in rows if r["station"] == "Goldstone"]
        xb = [r for r in gds if "X" in r["up_active_bands"]]
        print(f"{poll_utc} dt={ctx['dt_sec']:>5}s  Goldstone dishes={len(gds)} "
              f"X-band uplinks={len(xb)}", flush=True)
        for r in xb:
            print(f"    {r['dish']} -> {r['up_spacecraft']} "
                  f"P={r['up_x_power_kw']}kW az={r['azimuth_deg']} "
                  f"el={r['elevation_deg']} act={r['activity']}", flush=True)
    except Exception as exc:
        # raw XML is already on disk, so a parse failure is recoverable later
        print(f"{poll_utc} PARSE FAILED: {exc}", flush=True)


# --------------------------------------------------------------------------
def main():
    node = sys.argv[1] if len(sys.argv) > 1 else "once"

    if node == "once":
        poll_once()
        return

    hit = todays_pass(node)
    if hit is None:
        return
    pass_id, target = hit
    ctx = {"pass_id": pass_id, "node": node,
           "target_utc": target.strftime("%Y-%m-%dT%H:%M:%SZ")}

    start = target - timedelta(seconds=BEFORE_S)
    end = target + timedelta(seconds=AFTER_S)
    now = datetime.now(timezone.utc)
    print(f"pass {pass_id} target {ctx['target_utc']}  "
          f"window {start:%H:%M:%S}-{end:%H:%M:%S}Z", flush=True)

    if now > end:
        print("window already passed; single poll for the record", flush=True)
        poll_once(ctx, target)
        return
    if now < start:
        wait = (start - now).total_seconds()
        print(f"sleeping {wait:.0f}s until window start", flush=True)
        time.sleep(wait)

    while datetime.now(timezone.utc) <= end:
        poll_once(ctx, target)
        if datetime.now(timezone.utc) + timedelta(seconds=INTERVAL) > end:
            break
        time.sleep(INTERVAL)

    print("window complete", flush=True)


if __name__ == "__main__":
    main()
