"""GDELT 2.0: the 15-minute Events + Mentions stream (gdelt_v2) and the Global Knowledge Graph (gdelt_gkg).

GDELT 2.0 publishes three zipped files every 15 minutes (96 a day):
  YYYYMMDDHHMMSS.export.CSV.zip    every new event, 61 tab-separated columns
  YYYYMMDDHHMMSS.mentions.CSV.zip  every article that mentions an event (new or old), 16 columns
  YYYYMMDDHHMMSS.gkg.csv.zip       every article: themes, organizations, places, tone, 27 columns
...for English-language sources, plus the same three as YYYYMMDDHHMMSS.translation.*.zip for GDELT
Translingual (sources in 65 other languages, machine-translated). Both streams are counted together.

Raw volume is ~1-2 GB a day, far too big for the repo, so each complete UTC day is read in full and
boiled down to daily tables (the same doctrine as gdelt_events for GDELT 1.0). A day is the unit of
work: it is either fully counted or retried next run, and re-counting a day is idempotent.

  python -m tools.databank.gdelt_v2 --probe   # fetch the latest real files and sanity-check the parsers
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import zipfile

from . import common as C
from . import config as K
from . import gdelt_events as G1
from .sources import SLOW_LAST, SLOW_LAST_ORDER, source

BASE = "http://data.gdeltproject.org/gdeltv2/"
FIRST_DAY = dt.date(2015, 2, 19)   # GDELT 2.0 begins mid-Feb 2015
NIGHTLY_LOOKBACK = 3               # nightly re-checks the last 3 complete days (self-heals missed nights)
SLOTS = [f"{h:02d}{m:02d}00" for h in range(24) for m in (0, 15, 30, 45)]  # 96 per day
DAY_ERR_LIMIT = 4                  # non-404 failures in one day before giving the day up for this run
# GDELT publishes two parallel streams: English-language sources, and GDELT Translingual (machine-translated
# coverage from 65 languages) as YYYYMMDDHHMMSS.translation.<kind>.zip. Both are read and counted together.
VARIANTS = ("", "translation.")

# Events export, 0-based. Same as 1.0 except each geo block gained ADM2Code (+1 each), so the
# action-location country moves from 51 to 53, and DATEADDED/SOURCEURL follow at 59/60.
E_ID, E_ROOT, E_QUAD, E_GOLD, E_MENT, E_ART, E_TONE, E_CC, E_NCOLS = 0, 28, 29, 30, 31, 33, 34, 53, 61
# Mentions
M_EVT, M_EVTTIME, M_TIME, M_TYPE, M_SRC, M_CONF, M_TONE, M_TRANS, M_NCOLS = 0, 1, 2, 3, 4, 11, 13, 14, 16
# GKG 2.1
G_SRC, G_THEMES, G_LOCS, G_ORGS, G_TONE, G_TRANS, G_NCOLS = 3, 7, 9, 13, 15, 25, 27

THEMES_TOP, ORGS_TOP, SOURCES_TOP = 300, 300, 100
THEME_KEEP_PREFIXES = ("ECON_", "EPU_")  # economy + economic-policy-uncertainty themes: every day, always

MENTION_WORLD_COLS = ["date", "mentions", "events_mentioned", "sources", "tone", "confidence",
                      "web_share", "translated_share", "echo_share", "matched_share", "slots"]
MENTION_CC_COLS = ["date", "country", "mentions", "events_mentioned", "amplification", "tone"]
GKG_WORLD_COLS = ["date", "articles", "sources", "tone", "positive", "negative", "polarity",
                  "translated_share", "slots"]
GKG_COUNT_COLS = ["articles", "tone"]


# ---------------------------------------------------------------- parsing (pure, testable)
def _f(s):
    try:
        return float(s) if s else 0.0
    except ValueError:
        return None


def _i(s):
    try:
        return int(s) if s else 0
    except ValueError:
        return None


class EventsDay:
    """Aggregates a day's export files into gdelt_events-compatible rows (+ an event→country map)."""

    def __init__(self, day):
        self.d = day.strftime("%Y%m%d")
        self.agg = {}
        self.cc_of = {}  # GLOBALEVENTID -> action country, for joining that day's mentions
        self.bad = 0

    def add(self, lines):
        d = self.d
        for line in lines:
            f = line.rstrip("\r\n").split("\t")
            if len(f) < E_NCOLS - 1:  # SOURCEURL may be absent on rare rows
                self.bad += 1
                continue
            ment, art, tone, gold = _i(f[E_MENT]), _i(f[E_ART]), _f(f[E_TONE]), _f(f[E_GOLD])
            if None in (ment, art, tone, gold):
                self.bad += 1
                continue
            q, root, cc = f[E_QUAD], f[E_ROOT], f[E_CC].strip()
            if cc:
                self.cc_of[f[E_ID]] = cc
            wa, wm = max(art, 1), max(ment, 1)
            for key in ((d, "WORLD"), (d, cc)) if cc else ((d, "WORLD"),):
                a = self.agg.get(key)
                if a is None:
                    a = self.agg[key] = [0, 0, 0, 0.0, 0, 0.0, 0, 0, 0, 0, 0, 0, 0]
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
                elif root in G1.VIOLENCE:
                    a[12] += 1

    def rows(self):
        return G1.to_rows(self.agg)


class MentionsDay:
    """Every article mention of every event, that day. Shows how far and how fast stories travel."""

    def __init__(self, day, cc_of=None):
        self.d = day.strftime("%Y%m%d")
        self.cc_of = cc_of or {}
        self.n = self.web = self.trans = self.echo = self.matched = 0
        self.tone = self.conf = 0.0
        self.srcs, self.evts = {}, set()
        self.cc = {}  # cc -> [mentions, tone_sum, set(event ids)]
        self.bad = 0

    def add(self, lines):
        d = self.d
        for line in lines:
            f = line.rstrip("\r\n").split("\t")
            if len(f) < M_NCOLS - 1:
                self.bad += 1
                continue
            tone, conf = _f(f[M_TONE]), _f(f[M_CONF])
            if tone is None or conf is None:
                self.bad += 1
                continue
            eid = f[M_EVT]
            self.n += 1
            self.tone += tone
            self.conf += conf
            self.web += f[M_TYPE] == "1"
            self.trans += bool(f[M_TRANS].strip()) if len(f) > M_TRANS else 0
            self.echo += f[M_EVTTIME][:8] < d  # an older story still being covered today
            self.evts.add(eid)
            s = self.srcs.get(f[M_SRC])
            if s is None:
                s = self.srcs[f[M_SRC]] = [0, 0.0]
            s[0] += 1
            s[1] += tone
            cc = self.cc_of.get(eid)
            if cc:
                self.matched += 1
                c = self.cc.get(cc)
                if c is None:
                    c = self.cc[cc] = [0, 0.0, set()]
                c[0] += 1
                c[1] += tone
                c[2].add(eid)

    def rows(self, slots):
        date = f"{self.d[:4]}-{self.d[4:6]}-{self.d[6:]}"
        n = self.n
        world = [{"date": date, "mentions": n, "events_mentioned": len(self.evts), "sources": len(self.srcs),
                  "tone": round(self.tone / n, 3) if n else None,
                  "confidence": round(self.conf / n, 2) if n else None,
                  "web_share": round(self.web / n, 4) if n else None,
                  "translated_share": round(self.trans / n, 4) if n else None,
                  "echo_share": round(self.echo / n, 4) if n else None,
                  "matched_share": round(self.matched / n, 4) if n else None, "slots": slots}]
        by_cc = [{"date": date, "country": cc, "mentions": c[0], "events_mentioned": len(c[2]),
                  "amplification": round(c[0] / len(c[2]), 3), "tone": round(c[1] / c[0], 3)}
                 for cc, c in self.cc.items()]
        top = sorted(self.srcs.items(), key=lambda kv: (-kv[1][0], kv[0]))[:SOURCES_TOP]
        sources = [{"date": date, "source": s, "mentions": v[0], "tone": round(v[1] / v[0], 3)}
                   for s, v in top]
        return world, by_cc, sources


class GkgDay:
    """Every article GDELT read that day: tone, themes, organizations and the countries it covers."""

    def __init__(self, day, track_orgs=()):
        self.d = day.strftime("%Y%m%d")
        self.track = {o.lower() for o in track_orgs}
        self.n = self.trans = 0
        self.tone = self.pos = self.neg = self.pol = 0.0
        self.srcs = set()
        self.themes, self.orgs, self.cc = {}, {}, {}
        self.bad = 0

    @staticmethod
    def _bump(table, key, tone):
        v = table.get(key)
        if v is None:
            table[key] = [1, tone]
        else:
            v[0] += 1
            v[1] += tone

    def add(self, lines):
        for line in lines:
            f = line.rstrip("\r\n").split("\t")
            if len(f) < G_NCOLS - 1:
                self.bad += 1
                continue
            t = f[G_TONE].split(",")
            try:
                tone, pos, neg, pol = float(t[0]), float(t[1]), float(t[2]), float(t[3])
            except (ValueError, IndexError):
                self.bad += 1
                continue
            self.n += 1
            self.tone += tone
            self.pos += pos
            self.neg += neg
            self.pol += pol
            self.trans += bool(f[G_TRANS].strip()) if len(f) > G_TRANS else 0
            if f[G_SRC]:
                self.srcs.add(f[G_SRC])
            for th in {x for x in f[G_THEMES].split(";") if x}:
                self._bump(self.themes, th, tone)
            for org in {x.strip().lower() for x in f[G_ORGS].split(";") if x.strip()}:
                self._bump(self.orgs, org, tone)
            ccs = set()
            for loc in f[G_LOCS].split(";"):
                p = loc.split("#")
                if len(p) > 2 and p[2]:
                    ccs.add(p[2])
            for cc in ccs:
                self._bump(self.cc, cc, tone)

    def _top(self, table, n, keep):
        top = sorted(table.items(), key=lambda kv: (-kv[1][0], kv[0]))[:n]
        have = {k for k, _ in top}
        top += sorted((k, v) for k, v in table.items() if k not in have and keep(k))
        return top

    def rows(self, slots):
        date = f"{self.d[:4]}-{self.d[4:6]}-{self.d[6:]}"
        n = self.n
        r = lambda x: round(x / n, 3) if n else None
        world = [{"date": date, "articles": n, "sources": len(self.srcs), "tone": r(self.tone),
                  "positive": r(self.pos), "negative": r(self.neg), "polarity": r(self.pol),
                  "translated_share": round(self.trans / n, 4) if n else None, "slots": slots}]
        fmt = lambda col, items: [{"date": date, col: k, "articles": v[0], "tone": round(v[1] / v[0], 3)}
                                  for k, v in items]
        themes = fmt("theme", self._top(self.themes, THEMES_TOP, lambda k: k.startswith(THEME_KEEP_PREFIXES)))
        orgs = fmt("org", self._top(self.orgs, ORGS_TOP, lambda k: k in self.track))
        ccs = fmt("country", sorted(self.cc.items()))
        return world, ccs, themes, orgs


# ---------------------------------------------------------------- download one day
def _lines(raw):
    return G1._read_zip(raw)


def _day_files(ctx, day, kind, consume, t, variant=""):
    """Feed every 15-minute file of `kind` for `day` to consume(lines). Returns (slots_ok, missing, errs)."""
    ds = day.strftime("%Y%m%d")
    old = (ctx.today - day).days > 7
    ok, missing, errs = 0, 0, []
    for i, s in enumerate(SLOTS):
        if old and i == 8 and ok == 0 and missing == 8:
            return 0, len(SLOTS), errs  # an old day GDELT never published: don't knock 88 more times
        name = f"{ds}{s}.{variant}{kind}.zip"
        try:
            raw = C.fetch(BASE + name, timeout=180, retries=3)
        except C.FetchError as e:
            if "HTTP 404" in str(e):
                missing += 1
                continue
            errs.append(f"{name}: {e}")
            if len(errs) >= DAY_ERR_LIMIT:
                break
            continue
        try:
            consume(_lines(raw))
        except zipfile.BadZipFile:
            errs.append(f"{name}: corrupt zip")
            continue
        ok += 1
    return ok, missing, errs


def _both(ctx, day, kind, consume, t):
    """English + translated files for one kind. Returns (slots_en, slots_tr, worst_missing, errs)."""
    ok_en, miss_en, errs = _day_files(ctx, day, kind, consume, t, VARIANTS[0])
    if errs:
        return 0, 0, 0, errs
    ok_tr, miss_tr, errs = _day_files(ctx, day, kind, consume, t, VARIANTS[1])
    return ok_en, ok_tr, max(miss_en, miss_tr), errs


def _final(ctx, day, ok, missing):
    """A day with gaps is retried while GDELT may still publish them; after that, gaps are gaps.
    A day with nothing at all is retried for a week (an outage), then accepted as never published."""
    age = (ctx.today - day).days
    return missing == 0 or (age >= 2 and ok > 0) or age > 7


# ---------------------------------------------------------------- state
def _state_path(ctx):
    return os.path.join(ctx.bank, "state", "gdelt_v2.json")


def _load(ctx):
    p = _state_path(ctx)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save(ctx, st):
    p = _state_path(ctx)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({k: sorted(v) for k, v in st.items()}, f, indent=0)


def _pending(ctx, done, oldest):
    last = ctx.today - dt.timedelta(days=1)  # today is never complete
    first = max(oldest, FIRST_DAY)
    out, d = [], last
    while d >= first:
        if d.isoformat() not in done:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out  # newest first


def _by_month(rows):
    out = {}
    for r in rows:
        out.setdefault(r["date"][:7], []).append(r)
    return out


def _put(ctx, t, parts, rows, key, cols):
    t.add(C.upsert_csv(ctx.table("news", "gdelt2", *parts), rows, key=key, columns=cols))


def _put_monthly(ctx, t, folder, rows, key, cols):
    for ym, rs in _by_month(rows).items():
        _put(ctx, t, folder + [f"{ym}.csv"], rs, key, cols)


def _run(ctx, t, stream, budget_env, nightly_min, backfill_min, oldest_backfill, do_day):
    budget = float(ctx.env.get(budget_env, nightly_min if ctx.mode == "nightly" else backfill_min)) * 60
    t0 = time.monotonic()
    st = _load(ctx)
    done = set(st.get(stream, []))
    oldest = (ctx.today - dt.timedelta(days=NIGHTLY_LOOKBACK)) if ctx.mode == "nightly" else oldest_backfill
    pending = _pending(ctx, done, oldest)
    finished, partial, failed, records = 0, 0, [], 0
    for day in pending:
        if time.monotonic() - t0 > budget:
            break
        n, ok, missing, errs = do_day(day)
        if errs:
            failed.append(f"{day}: {errs[0]}")
            if len(failed) >= 2:
                break  # server unreachable tonight; don't burn the budget
            continue
        records += n
        if _final(ctx, day, ok, missing):
            done.add(day.isoformat())
            finished += 1
        else:
            partial += 1
        if missing:
            t.notes.append(f"{day}: {missing}/96 files not published" + ("" if ok else " (whole day absent)"))
        st[stream] = done
        _save(ctx, st)  # checkpoint after every day
    st[stream] = done
    _save(ctx, st)
    left = len(_pending(ctx, done, oldest))
    t.notes.insert(0, f"{finished} days complete, {partial} partial, {records:,} records read this run"
                      f" · {len(done)} days banked · {left} left in window")
    t.notes.extend(failed[:4])
    if failed and not (finished or partial):
        raise C.FetchError(f"data.gdeltproject.org/gdeltv2 unreachable — {failed[0]}")


# ---------------------------------------------------------------- sources
@source("gdelt_v2")
def gdelt_v2(ctx, t):
    """Events + Mentions, one day at a time: events first, so that day's mentions can be tied to a country."""

    def do_day(day):
        ev = EventsDay(day)
        ok_e, tr_e, miss_e, errs = _both(ctx, day, "export.CSV", ev.add, t)
        if errs:
            return 0, 0, 0, errs
        mn = MentionsDay(day, ev.cc_of)
        ok_m, tr_m, miss_m, errs = _both(ctx, day, "mentions.CSV", mn.add, t)
        if errs:
            return 0, 0, 0, errs
        erows = ev.rows()
        if erows:
            world = [dict(r, slots=ok_e, slots_translated=tr_e) for r in erows if r["country"] == "WORLD"]
            _put(ctx, t, ["events", "world_daily.csv"], world, ("date", "country"),
                 G1.COLS + ["slots", "slots_translated"])
            _put_monthly(ctx, t, ["events", "by_country"], [r for r in erows if r["country"] != "WORLD"],
                         ("date", "country"), G1.COLS)
        if mn.n:
            w, cc, srcs = mn.rows(ok_m)
            w[0]["slots_translated"] = tr_m
            _put(ctx, t, ["mentions", "world_daily.csv"], w, ("date",), MENTION_WORLD_COLS + ["slots_translated"])
            _put_monthly(ctx, t, ["mentions", "by_country"], cc, ("date", "country"), MENTION_CC_COLS)
            _put_monthly(ctx, t, ["mentions", "top_sources"], srcs, ("date", "source"),
                         ["date", "source", "mentions", "tone"])
        for name, bad in (("events", ev.bad), ("mentions", mn.bad)):
            if bad:
                t.notes.append(f"{day} {name}: skipped {bad} malformed lines")
        events = sum(r["events"] for r in erows if r["country"] == "WORLD")
        return events + mn.n, min(ok_e, ok_m) + min(tr_e, tr_m), max(miss_e, miss_m), []

    _run(ctx, t, "core", "GDELT_V2_MINUTES", "20", "280", FIRST_DAY, do_day)


@source("gdelt_gkg")
def gdelt_gkg(ctx, t):
    """Global Knowledge Graph: the heaviest stream (~1 GB a day). Backfill only reaches back
    GDELT_GKG_HISTORY_DAYS (default 30) — a full 2015+ history would be terabytes to download."""

    def do_day(day):
        g = GkgDay(day, K.GKG_TRACK_ORGS)
        ok, tr, miss, errs = _both(ctx, day, "gkg.csv", g.add, t)
        if errs:
            return 0, 0, 0, errs
        if g.n:
            w, cc, themes, orgs = g.rows(ok)
            w[0]["slots_translated"] = tr
            _put(ctx, t, ["gkg", "world_daily.csv"], w, ("date",), GKG_WORLD_COLS + ["slots_translated"])
            _put_monthly(ctx, t, ["gkg", "by_country"], cc, ("date", "country"), ["date", "country"] + GKG_COUNT_COLS)
            _put_monthly(ctx, t, ["gkg", "themes"], themes, ("date", "theme"), ["date", "theme"] + GKG_COUNT_COLS)
            _put_monthly(ctx, t, ["gkg", "orgs"], orgs, ("date", "org"), ["date", "org"] + GKG_COUNT_COLS)
        if g.bad:
            t.notes.append(f"{day} gkg: skipped {g.bad} malformed lines")
        return g.n, ok + tr, miss, []

    hist = int(ctx.env.get("GDELT_GKG_HISTORY_DAYS", "30"))
    _run(ctx, t, "gkg", "GDELT_GKG_MINUTES", "30", "280", ctx.today - dt.timedelta(days=hist), do_day)


SLOW_LAST.update({"gdelt_v2", "gdelt_gkg"})
SLOW_LAST_ORDER[:] = ["gdelt_events", "gdelt_v2", "gdelt_gkg", "gdelt"]


# ---------------------------------------------------------------- live probe
def _prev_slot_url(url):
    name = url.rsplit("/", 1)[1]
    t = dt.datetime.strptime(name[:14], "%Y%m%d%H%M%S") - dt.timedelta(minutes=15)
    return BASE + t.strftime("%Y%m%d%H%M%S") + name[14:]


def probe():
    """Fetch GDELT's newest real files and prove every parser reads them. Exit 1 on any doubt."""
    rc = _probe_one("lastupdate.txt", "English")
    rc |= _probe_one("lastupdate-translation.txt", "Translingual (65 languages)")
    print("PROBE", "FAILED" if rc else "PASSED")
    return rc


def _probe_one(listing, label):
    print(f"--- {label}: {listing}")
    txt = C.fetch_text(BASE + listing, timeout=60)
    print(txt.strip())
    urls = {}
    for line in txt.splitlines():
        p = line.split()
        if len(p) == 3:
            for kind in ("export", "mentions", "gkg"):
                if f".{kind}." in p[2]:
                    urls[kind] = (p[2], int(p[0]))
    problems = []
    if set(urls) != {"export", "mentions", "gkg"}:
        print(listing, txt)
        return 1
    day = dt.date.today()
    try:
        raw = {k: C.fetch(u, timeout=180) for k, (u, _) in urls.items()}
    except C.FetchError as e:  # lastupdate.txt can name a file a few seconds before it is served
        print(f"newest slot not served yet ({e}); using the slot before it")
        prev = lambda u: _prev_slot_url(u)
        urls = {k: (prev(u), None) for k, (u, _) in urls.items()}
        try:
            raw = {k: C.fetch(u, timeout=180) for k, (u, _) in urls.items()}
        except C.FetchError as e2:
            print(f"PROBLEM: {label}: listed files are not downloadable ({e2})")
            return 1
    for k, (u, size) in urls.items():
        print(f"{k:9s} {len(raw[k]) / 1e6:7.2f} MB  {u}")
        if size is not None and len(raw[k]) != size:
            problems.append(f"{k}: got {len(raw[k])} bytes, lastupdate says {size}")

    ev = EventsDay(day)
    ev.add(_lines(raw["export"]))
    rows = ev.rows()
    world = next((r for r in rows if r["country"] == "WORLD"), None)
    ccs = [r["country"] for r in rows if r["country"] != "WORLD"]
    print(f"events    {world and world['events']} events · {len(ccs)} countries · bad {ev.bad}"
          f" · tone {world and world['tone']} · goldstein {world and world['goldstein']}")
    if not world or world["events"] < 50:
        problems.append("events: too few rows parsed")
    if ev.bad > (world["events"] if world else 0) * 0.02:
        problems.append(f"events: {ev.bad} malformed lines")
    if ccs and sum(len(c) == 2 and c.isalpha() for c in ccs) / len(ccs) < 0.95:
        problems.append(f"events: country column looks wrong: {ccs[:10]}")
    if world and not (-30 < (world["tone"] or 0) < 30 and -10 <= (world["goldstein"] or 0) <= 10):
        problems.append("events: tone/goldstein out of range — column offsets wrong?")

    mn = MentionsDay(day, ev.cc_of)
    mn.add(_lines(raw["mentions"]))
    w, cc, srcs = mn.rows(1)
    print(f"mentions  {mn.n} mentions · {len(mn.evts)} events · {len(mn.srcs)} sources · matched "
          f"{w[0]['matched_share']} · tone {w[0]['tone']} · conf {w[0]['confidence']} · bad {mn.bad}")
    print("          top sources:", ", ".join(s["source"] for s in srcs[:5]))
    if mn.n < 50 or mn.bad > mn.n * 0.02:
        problems.append(f"mentions: {mn.n} parsed, {mn.bad} malformed")
    if not (0 < (w[0]["confidence"] or 0) <= 100):
        problems.append("mentions: confidence out of range — column offsets wrong?")
    if (w[0]["matched_share"] or 0) < 0.2:  # informational: many mentions are of older events
        print("NOTE: few of this slot's mentions point at this slot's own events (normal for one slot)")

    g = GkgDay(day, K.GKG_TRACK_ORGS)
    g.add(_lines(raw["gkg"]))
    gw, gcc, themes, orgs = g.rows(1)
    print(f"gkg       {g.n} articles · {len(g.themes)} themes · {len(g.orgs)} orgs · {len(g.cc)} countries"
          f" · tone {gw[0]['tone']} · bad {g.bad}")
    print("          top themes:", ", ".join(x["theme"] for x in themes[:6]))
    print("          top orgs:  ", ", ".join(x["org"] for x in orgs[:6]))
    if g.n < 50 or g.bad > g.n * 0.02:
        problems.append(f"gkg: {g.n} parsed, {g.bad} malformed")
    if len(g.themes) < 50 or len(g.cc) < 10:
        problems.append("gkg: too few themes/countries — column offsets wrong?")
    if "Translingual" in label and (gw[0]["translated_share"] or 0) < 0.5:
        problems.append("gkg: translation stream should be mostly translated articles")

    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


if __name__ == "__main__":
    a = argparse.ArgumentParser()
    a.add_argument("--probe", action="store_true")
    if a.parse_args().probe:
        sys.exit(probe())
