"""GDELT 1.0 Event Database — raw files from data.gdeltproject.org (no API, no request throttle).

GDELT machine-reads world news and codes every event: who, what, where, tone, conflict vs cooperation.
  1979–2005   one file per year
  2006–2013/3 one file per month
  2013/4 →    one file per day

We keep a daily summary, worldwide and for each country (by where the event happened):
  events, mentions, articles, tone (article-weighted), goldstein (-10 conflict … +10 cooperation,
  mention-weighted), quad1..4 (verbal coop, material coop, verbal conflict, material conflict),
  protests (CAMEO root 14), violence (roots 18-20), conflict_share.

Resumable: a cursor in databank/state/ records finished files. Each run works newest-first until its
time budget is spent, so history fills backward from today across runs and then just adds each new day.
"""
import datetime as dt
import io
import json
import os
import time
import zipfile

from . import common as C
from .sources import source, SLOW_LAST

BASE = "http://data.gdeltproject.org/events/"

# GDELT 1.0 column positions (0-based, tab-separated, no header)
SQLDATE, QUAD, ROOT, GOLD, MENT, ART, TONE, GEO_CC = 1, 29, 28, 30, 31, 33, 34, 51
SPLIT = GEO_CC + 1
VIOLENCE = {"18", "19", "20"}
NOT_YET_DAYS = 3  # GDELT publishes a day's file the next morning; very recent 404s are just "not out yet"

COLS = ["date", "country", "events", "mentions", "articles", "tone", "goldstein",
        "quad1", "quad2", "quad3", "quad4", "protests", "violence", "conflict_share"]


def file_list(today):
    out = [f"{y}.zip" for y in range(1979, 2006)]
    y, m = 2006, 1
    while (y, m) <= (2013, 3):
        out.append(f"{y}{m:02d}.zip")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    d = dt.date(2013, 4, 1)
    while d < today:
        out.append(d.strftime("%Y%m%d") + ".export.CSV.zip")
        d += dt.timedelta(days=1)
    return out


def file_date(name):
    """Daily files carry their own date; historical files don't."""
    return name[:8] if name.endswith(".export.CSV.zip") else None


def aggregate(lines, fixed_date=None):
    """Daily files: every event counts toward the file's news day. Historical files: by event date."""
    agg, bad = {}, 0
    for line in lines:
        f = line.split("\t", SPLIT)
        if len(f) <= GEO_CC:
            bad += 1
            continue
        d = fixed_date or f[SQLDATE]
        if len(d) != 8 or not d.isdigit():
            bad += 1
            continue
        try:
            ment = int(f[MENT] or 0)
            art = int(f[ART] or 0)
            tone = float(f[TONE] or 0)
            gold = float(f[GOLD] or 0)
        except ValueError:
            bad += 1
            continue
        q, root, cc = f[QUAD], f[ROOT], f[GEO_CC].strip()
        wa, wm = max(art, 1), max(ment, 1)
        for key in ((d, "WORLD"), (d, cc)) if cc else ((d, "WORLD"),):
            a = agg.get(key)
            if a is None:
                a = agg[key] = [0, 0, 0, 0.0, 0, 0.0, 0, 0, 0, 0, 0, 0, 0]
            a[0] += 1
            a[1] += ment
            a[2] += art
            a[3] += tone * wa
            a[4] += wa
            a[5] += gold * wm
            a[6] += wm
            if q in ("1", "2", "3", "4"):
                a[6 + int(q)] += 1
            if root == "14":
                a[11] += 1
            elif root in VIOLENCE:
                a[12] += 1
    return agg, bad


def to_rows(agg):
    out = []
    for (d, cc), a in agg.items():
        n = a[0]
        out.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "country": cc, "events": n,
                    "mentions": a[1], "articles": a[2],
                    "tone": round(a[3] / a[4], 3) if a[4] else None,
                    "goldstein": round(a[5] / a[6], 3) if a[6] else None,
                    "quad1": a[7], "quad2": a[8], "quad3": a[9], "quad4": a[10],
                    "protests": a[11], "violence": a[12],
                    "conflict_share": round((a[9] + a[10]) / n, 4) if n else None})
    return out


def _read_zip(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for member in z.namelist():
            with z.open(member) as fh:
                for line in io.TextIOWrapper(fh, encoding="utf-8", errors="replace"):
                    yield line


def _load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        return set(s.get("done", [])), set(s.get("missing", []))
    return set(), set()


def _save_state(path, done, missing):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(done), "missing": sorted(missing)}, f, indent=0)


def store(ctx, t, rows):
    world = [r for r in rows if r["country"] == "WORLD"]
    t.add(C.upsert_csv(ctx.table("news", "gdelt_events", "world_daily.csv"), world,
                       key=("date", "country"), columns=COLS))
    by_month = {}
    for r in rows:
        if r["country"] != "WORLD":
            by_month.setdefault(r["date"][:7], []).append(r)
    for ym, rs in by_month.items():
        t.add(C.upsert_csv(ctx.table("news", "gdelt_events", "by_country", f"{ym}.csv"), rs,
                           key=("date", "country"), columns=COLS))


@source("gdelt_events")
def gdelt_events(ctx, t):
    default = "15" if ctx.mode == "nightly" else "280"
    budget = float(ctx.env.get("GDELT_EVENTS_MINUTES", default)) * 60
    t0 = time.monotonic()
    state_path = os.path.join(ctx.bank, "state", "gdelt_events.json")
    done, missing = _load_state(state_path)
    names = file_list(ctx.today)
    pending = [n for n in reversed(names) if n not in done and n not in missing]  # newest first
    if ctx.mode == "nightly":  # big yearly/monthly files belong to backfill runs
        pending = [n for n in pending if file_date(n)]
    processed, errs, rows_in = 0, [], 0
    for name in pending:
        if time.monotonic() - t0 > budget:
            break
        fd = file_date(name)
        try:
            raw = C.fetch(BASE + name, timeout=900, retries=3)
        except C.FetchError as e:
            if "HTTP 404" in str(e):
                if fd and (ctx.today - dt.date(int(fd[:4]), int(fd[4:6]), int(fd[6:]))).days <= NOT_YET_DAYS:
                    continue  # not published yet — try again next run
                missing.add(name)
                continue
            errs.append(f"{name}: {e}")
            if len(errs) >= 3:
                break  # server unreachable tonight; don't burn the budget
            continue
        try:
            agg, bad = aggregate(_read_zip(raw), fixed_date=fd)
        except zipfile.BadZipFile:
            errs.append(f"{name}: corrupt zip")
            continue
        rows = to_rows(agg)
        rows_in += sum(r["events"] for r in rows if r["country"] == "WORLD")
        store(ctx, t, rows)
        done.add(name)
        processed += 1
        if bad:
            t.notes.append(f"{name}: skipped {bad} malformed lines")
        _save_state(state_path, done, missing)  # checkpoint after every file
    _save_state(state_path, done, missing)
    left = len([n for n in names if n not in done and n not in missing])
    t.notes.insert(0, f"{processed} files, {rows_in:,} events this run · {len(done)}/{len(names)} files done"
                      f" · {left} left" + (f" · {len(missing)} missing at source" if missing else ""))
    t.notes.extend(errs[:5])
    if errs and not processed:
        raise C.FetchError(f"data.gdeltproject.org unreachable — {errs[0]}")


SLOW_LAST.add("gdelt_events")
