"""Every feed the bank collects. Each source is isolated: one failing never stops the others.

Modes:
  backfill — pull the deepest history each source offers (run once, re-runnable safely)
  nightly  — pull a recent overlapping window and upsert (self-heals missed nights)
"""
import asyncio
import datetime as dt
import json
import os
import time
import urllib.parse

from . import common as C
from . import config as K

REGISTRY = []


def source(name, modes=("nightly", "backfill")):
    def wrap(fn):
        REGISTRY.append((name, set(modes), fn))
        return fn
    return wrap


class Tally:
    def __init__(self):
        self.added = self.updated = 0
        self.notes = []
        self.skipped = False

    def skip(self, why):
        self.skipped = True
        self.notes.append(why)

    def add(self, res):
        a, u, _ = res
        self.added += a
        self.updated += u
        return res


# ====================================================================== FRED
@source("fred")
def fred(ctx, t):
    since = None if ctx.mode == "backfill" else ctx.iso(120)
    fails = []
    for sid in K.FRED_SERIES:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
        if since:
            url += f"&cosd={since}"
        try:
            rows = C.read_csv_text(C.fetch_text(url))
        except C.FetchError as e:
            fails.append(f"{sid}: {e}")
            continue
        out = []
        for r in rows:
            d = r.get("observation_date") or r.get("DATE") or r.get("date")
            v = C.num(r.get(sid))
            if d and v is not None:
                out.append({"date": d, "value": v})
        t.add(C.upsert_csv(ctx.table("macro", "fred", f"{sid}.csv"), out, key=("date",)))
        C.pause(0.4)
    _partial(t, fails, len(K.FRED_SERIES))


# ====================================================================== US TREASURY
YC_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
          "daily-treasury-rates.csv/{y}/all?type={kind}&field_tdr_date_value={y}&page&_format=csv")


def _yc_rows(text):
    out = []
    for r in C.read_csv_text(text):
        raw = r.get("Date") or r.get("date")
        if not raw:
            continue
        try:
            d = dt.datetime.strptime(raw.strip(), "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        row = {"date": d}
        for k, v in r.items():
            if k and k.lower() != "date":
                row[k.lower().replace(" ", "").replace(".", "_")] = C.num(v)
        out.append(row)
    return out


@source("treasury_yields")
def treasury_yields(ctx, t):
    y_now = ctx.today.year
    fails, total = [], 0
    for kind, first, fname in (("daily_treasury_yield_curve", 1990, "yield_curve_nominal"),
                               ("daily_treasury_real_yield_curve", 2003, "yield_curve_real")):
        years = range(first, y_now + 1) if ctx.mode == "backfill" else \
            ([y_now - 1, y_now] if ctx.today.month == 1 else [y_now])
        total += len(years)
        for y in years:
            try:
                rows = _yc_rows(C.fetch_text(YC_URL.format(y=y, kind=kind)))
            except C.FetchError as e:
                fails.append(f"{fname} {y}: {e}")
                continue
            t.add(C.upsert_csv(ctx.table("rates", "treasury", f"{fname}.csv"), rows, key=("date",)))
            C.pause(0.5)
    _partial(t, fails, total)


FD = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"


def _fiscal_pages(endpoint, fields, size, max_pages):
    page, out = 1, []
    while page <= max_pages:
        url = (f"{FD}{endpoint}?fields={','.join(fields)}&sort=-record_date"
               f"&page[number]={page}&page[size]={size}")
        j = C.fetch_json(url)
        data = j.get("data") or []
        out.extend(data)
        total = int((j.get("meta") or {}).get("total-pages") or 1)
        if page >= total or not data:
            break
        page += 1
        C.pause(0.3)
    return out


@source("treasury_debt")
def treasury_debt(ctx, t):
    full = ctx.mode == "backfill"
    rows = _fiscal_pages("/v2/accounting/od/debt_to_penny",
                         ["record_date", "debt_held_public_amt", "intragov_hold_amt", "tot_pub_debt_out_amt"],
                         10000 if full else 60, 10 if full else 1)
    out = [{"date": r["record_date"], "debt_public": C.num(r.get("debt_held_public_amt")),
            "debt_intragov": C.num(r.get("intragov_hold_amt")),
            "debt_total": C.num(r.get("tot_pub_debt_out_amt"))} for r in rows]
    t.add(C.upsert_csv(ctx.table("rates", "treasury", "debt_to_penny.csv"), out, key=("date",)))

    rows = _fiscal_pages("/v2/accounting/od/avg_interest_rates",
                         ["record_date", "security_type_desc", "security_desc", "avg_interest_rate_amt"],
                         10000 if full else 200, 10 if full else 1)
    out = [{"date": r["record_date"], "security_type": r.get("security_type_desc"),
            "security": r.get("security_desc"), "avg_rate": C.num(r.get("avg_interest_rate_amt"))}
           for r in rows]
    t.add(C.upsert_csv(ctx.table("rates", "treasury", "avg_interest_rates.csv"), out,
                       key=("date", "security_type", "security")))


# ====================================================================== FX (ECB via Frankfurter)
FX_HOSTS = ["https://api.frankfurter.dev/v1", "https://api.frankfurter.app"]


def _fx_range(start, end):
    last = None
    for host in FX_HOSTS:
        try:
            return C.fetch_json(f"{host}/{start}..{end}?base=USD")
        except C.FetchError as e:
            last = e
    raise last


@source("fx_ecb")
def fx_ecb(ctx, t):
    if ctx.mode == "backfill":
        spans = [(f"{y}-01-01" if y > 1999 else "1999-01-04", f"{y}-12-31")
                 for y in range(1999, ctx.today.year + 1)]
    else:
        spans = [(ctx.iso(14), ctx.iso())]
    fails = []
    for a, b in spans:
        try:
            j = _fx_range(a, b)
        except C.FetchError as e:
            fails.append(f"{a}: {e}")
            continue
        rows = [{"date": d, **{k: v for k, v in rates.items()}} for d, rates in (j.get("rates") or {}).items()]
        t.add(C.upsert_csv(ctx.table("fx", "ecb_per_usd.csv"), rows, key=("date",)))
        C.pause(0.3)
    _partial(t, fails, len(spans))


# ====================================================================== WORLD BANK
@source("worldbank")
def worldbank(ctx, t):
    if ctx.mode == "nightly" and ctx.today.weekday() != 6:
        t.skip("weekly (Sundays) — skipped")
        return
    span = f"1960:{ctx.today.year}" if ctx.mode == "backfill" else f"{ctx.today.year - 6}:{ctx.today.year}"
    fails = []
    for ind in K.WB_INDICATORS:
        page, rows = 1, []
        try:
            while True:
                j = C.fetch_json(f"https://api.worldbank.org/v2/country/all/indicator/{ind}"
                                 f"?format=json&date={span}&per_page=20000&page={page}")
                if not isinstance(j, list) or len(j) < 2 or j[1] is None:
                    break
                for r in j[1]:
                    if r.get("value") is None:
                        continue
                    rows.append({"iso3": r.get("countryiso3code") or (r.get("country") or {}).get("id"),
                                 "year": r.get("date"), "country": (r.get("country") or {}).get("value"),
                                 "value": r.get("value")})
                if page >= int(j[0].get("pages") or 1):
                    break
                page += 1
        except C.FetchError as e:
            fails.append(f"{ind}: {e}")
            continue
        t.add(C.upsert_csv(ctx.table("macro", "worldbank", f"{ind}.csv"), rows, key=("iso3", "year")))
        C.pause(0.3)
    _partial(t, fails, len(K.WB_INDICATORS))


# ====================================================================== SEC EDGAR
def _sec_headers(ctx):
    ua = ctx.env.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise C.FetchError("SEC_USER_AGENT not set — SEC requires a contact (name + email). See databank/README.md")
    return {"User-Agent": ua}


@source("sec_filings", modes=("nightly", "backfill"))
def sec_filings(ctx, t):
    h = _sec_headers(ctx)
    rows, fails = [], []
    for slug, cik in K.CIKS.items():
        try:
            j = C.fetch_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers=h)
        except C.FetchError as e:
            fails.append(f"{slug}: {e}")
            continue
        rec = (j.get("filings") or {}).get("recent") or {}
        acc = rec.get("accessionNumber") or []
        for i, a in enumerate(acc):
            g = lambda k: (rec.get(k) or [None] * len(acc))[i]
            rows.append({"filing_date": g("filingDate"), "accession": a, "slug": slug, "cik": cik,
                         "form": g("form"), "report_date": g("reportDate"),
                         "primary_document": g("primaryDocument"), "description": g("primaryDocDescription")})
        C.pause(0.15)
    t.add(C.upsert_csv(ctx.table("companies", "sec_filings.csv"), rows, key=("filing_date", "accession")))
    _partial(t, fails, len(K.CIKS))


@source("sec_financials")
def sec_financials(ctx, t):
    if ctx.mode == "nightly" and ctx.today.weekday() != 5:
        t.skip("weekly (Saturdays) — skipped")
        return
    h = _sec_headers(ctx)
    fails = []
    for slug, cik in K.CIKS.items():
        try:
            j = C.fetch_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", headers=h)
        except C.FetchError as e:
            fails.append(f"{slug}: {e}")
            continue
        facts = j.get("facts") or {}
        rows = []
        for ns, concepts in (("us-gaap", K.SEC_CONCEPTS), ("dei", K.SEC_DEI)):
            for concept in concepts:
                node = (facts.get(ns) or {}).get(concept)
                if not node:
                    continue
                for unit, items in (node.get("units") or {}).items():
                    for x in items:
                        rows.append({"concept": concept, "unit": unit, "start": x.get("start") or "-",
                                     "end": x.get("end"), "accn": x.get("accn"), "value": x.get("val"),
                                     "fy": x.get("fy"), "fp": x.get("fp"), "form": x.get("form"),
                                     "filed": x.get("filed"), "frame": x.get("frame")})
        t.add(C.upsert_csv(ctx.table("companies", "sec_financials", f"{slug}.csv"), rows,
                           key=("concept", "unit", "start", "end", "accn")))
        C.pause(0.15)
    _partial(t, fails, len(K.CIKS))


# ====================================================================== CRYPTO
CB = "https://api.exchange.coinbase.com/products/{p}/candles?granularity=86400&start={a}&end={b}"


def _cb_rows(arr):
    out = []
    for c in arr or []:
        ts, low, high, opn, close, vol = c[:6]
        out.append({"date": dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat(),
                    "open": opn, "high": high, "low": low, "close": close, "volume": vol})
    return out


@source("crypto_coinbase")
def crypto_coinbase(ctx, t):
    fails = []
    for p in K.CRYPTO_PRODUCTS:
        path = ctx.table("crypto", "coinbase", f"{p}.csv")
        end = ctx.today + dt.timedelta(days=1)
        empty_streak, windows = 0, 0
        try:
            while True:
                start = end - dt.timedelta(days=299 if ctx.mode == "backfill" else 10)
                arr = C.fetch_json(CB.format(p=p, a=start.isoformat(), b=end.isoformat()))
                rows = _cb_rows(arr)
                # never store today's still-forming candle
                rows = [r for r in rows if r["date"] < ctx.today.isoformat()]
                t.add(C.upsert_csv(path, rows, key=("date",)))
                windows += 1
                if ctx.mode == "nightly":
                    break
                empty_streak = empty_streak + 1 if not arr else 0
                if empty_streak >= 2 or start.year < 2012:
                    break
                end = start
                C.pause(0.25)
        except C.FetchError as e:
            fails.append(f"{p}: {e}")
        C.pause(0.2)
    _partial(t, fails, len(K.CRYPTO_PRODUCTS))


@source("crypto_coingecko", modes=("nightly",))
def crypto_coingecko(ctx, t):
    url = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc"
           "&per_page=100&page=1&price_change_percentage=24h,7d")
    j = C.fetch_json(url)
    ctx.save_raw("coingecko_markets.json", j)
    d = ctx.iso()
    rows = [{"date": d, "id": c.get("id"), "symbol": c.get("symbol"), "rank": c.get("market_cap_rank"),
             "price": c.get("current_price"), "market_cap": c.get("market_cap"),
             "volume_24h": c.get("total_volume"), "chg_24h_pct": c.get("price_change_percentage_24h"),
             "chg_7d_pct": c.get("price_change_percentage_7d_in_currency"),
             "supply": c.get("circulating_supply")} for c in j or []]
    t.add(C.upsert_csv(ctx.table("crypto", "coingecko_top100_daily.csv"), rows, key=("date", "id")))
    try:
        g = C.fetch_json("https://api.coingecko.com/api/v3/global").get("data") or {}
        t.add(C.upsert_csv(ctx.table("crypto", "global_daily.csv"), [{
            "date": d, "total_mcap_usd": (g.get("total_market_cap") or {}).get("usd"),
            "total_volume_usd": (g.get("total_volume") or {}).get("usd"),
            "btc_dominance": (g.get("market_cap_percentage") or {}).get("btc"),
            "eth_dominance": (g.get("market_cap_percentage") or {}).get("eth"),
            "active_coins": g.get("active_cryptocurrencies")}], key=("date",)))
    except C.FetchError as e:
        t.notes.append(f"global: {e}")


# ====================================================================== BITCOIN CHAIN
MP = "https://mempool.space/api"


@source("bitcoin_chain")
def bitcoin_chain(ctx, t):
    if ctx.mode == "backfill":
        j = C.fetch_json(f"{MP}/v1/mining/hashrate/all")
        hr = [{"date": dt.datetime.fromtimestamp(x["timestamp"], dt.timezone.utc).date().isoformat(),
               "hashrate": x.get("avgHashrate")} for x in j.get("hashrates") or []]
        t.add(C.upsert_csv(ctx.table("crypto", "btc_hashrate.csv"), hr, key=("date",)))
        df = [{"height": x.get("height"), "date": dt.datetime.fromtimestamp(x["time"], dt.timezone.utc)
               .date().isoformat(), "difficulty": x.get("difficulty"), "adjustment": x.get("adjustment")}
              for x in j.get("difficulty") or []]
        t.add(C.upsert_csv(ctx.table("crypto", "btc_difficulty.csv"), df, key=("height",)))
        return
    fees = C.fetch_json(f"{MP}/v1/fees/recommended")
    mem = C.fetch_json(f"{MP}/mempool")
    tip = int(C.fetch_text(f"{MP}/blocks/tip/height").strip())
    ctx.save_raw("btc_fees_mempool.json", {"fees": fees, "mempool": {k: mem.get(k) for k in
                                                                        ("count", "vsize", "total_fee")}})
    blocks, h = [], tip
    for _ in range(11):  # ~165 blocks covers a full day (~144) with overlap
        arr = C.fetch_json(f"{MP}/v1/blocks/{h}")
        if not arr:
            break
        for b in arr:
            ex = b.get("extras") or {}
            blocks.append({"height": b.get("height"),
                           "date": dt.datetime.fromtimestamp(b.get("timestamp", 0), dt.timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "tx_count": b.get("tx_count"), "size": b.get("size"), "weight": b.get("weight"),
                           "total_fees": ex.get("totalFees"), "median_fee": ex.get("medianFee"),
                           "reward": ex.get("reward"), "pool": (ex.get("pool") or {}).get("name")})
        h = min(b["height"] for b in arr) - 1
        C.pause(0.3)
    t.add(C.upsert_csv(ctx.table("crypto", "btc_blocks.csv"), blocks, key=("height",)))
    t.add(C.upsert_csv(ctx.table("crypto", "btc_daily.csv"), [{
        "date": ctx.iso(), "tip_height": tip, "fee_fastest": fees.get("fastestFee"),
        "fee_halfhour": fees.get("halfHourFee"), "fee_hour": fees.get("hourFee"),
        "fee_economy": fees.get("economyFee"), "mempool_count": mem.get("count"),
        "mempool_vsize": mem.get("vsize"), "mempool_total_fee": mem.get("total_fee")}], key=("date",)))


# ====================================================================== LONG MARKET HISTORY (Stooq)
@source("markets_stooq")
def markets_stooq(ctx, t):
    fails = []
    for sym, label in K.STOOQ_SYMBOLS.items():
        url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(sym)}&i=d"
        if ctx.mode == "nightly":
            url += f"&d1={ctx.iso(30).replace('-', '')}&d2={ctx.iso().replace('-', '')}"
        try:
            text = C.fetch_text(url)
        except C.FetchError as e:
            fails.append(f"{label}: {e}")
            continue
        if not text.lower().startswith("date"):
            fails.append(f"{label}: unexpected response {text[:60]!r}")
            continue
        rows = [{"date": r.get("Date"), "open": C.num(r.get("Open")), "high": C.num(r.get("High")),
                 "low": C.num(r.get("Low")), "close": C.num(r.get("Close")), "volume": C.num(r.get("Volume"))}
                for r in C.read_csv_text(text) if r.get("Date")]
        t.add(C.upsert_csv(ctx.table("markets", "stooq", f"{label}.csv"), rows, key=("date",)))
        C.pause(1.0)
    _partial(t, fails, len(K.STOOQ_SYMBOLS))


# ====================================================================== NEWS & SOCIAL
GD = "https://api.gdeltproject.org/api/v2/doc/doc?query={q}&mode={m}&format=json&timespan={s}"


def _gdelt_date(sd):
    return f"{sd[0:4]}-{sd[4:6]}-{sd[6:8]}" if sd and len(sd) >= 8 else None


@source("gdelt")
def gdelt(ctx, t):
    span = "3months" if ctx.mode == "backfill" else "3d"
    fails, daily = [], {}
    for theme, q in K.GDELT_THEMES.items():
        qq = urllib.parse.quote(q + " sourcelang:eng")
        for mode, col in (("timelinetone", "tone"), ("timelinevol", "volume_pct")):
            try:
                j = C.fetch_json(GD.format(q=qq, m=mode, s=span))
                for series in j.get("timeline") or []:
                    for p in series.get("data") or []:
                        d = _gdelt_date(p.get("date"))
                        if d and p.get("value") is not None:
                            daily.setdefault((d, theme), {}).setdefault(col, []).append(p["value"])
            except C.FetchError as e:
                fails.append(f"{theme}/{mode}: {e}")
            C.pause(5.5)  # GDELT asks for one request per 5s
    rows = [{"date": d, "theme": th, **{c: round(sum(v) / len(v), 5) for c, v in cols.items()}}
            for (d, th), cols in daily.items() if d < ctx.iso()]  # only completed days
    t.add(C.upsert_csv(ctx.table("news", "gdelt_daily.csv"), rows, key=("date", "theme")))

    if ctx.mode == "nightly":
        recs, counts = [], []
        for theme, q in K.GDELT_THEMES.items():
            qq = urllib.parse.quote(q + " sourcelang:eng")
            try:
                j = C.fetch_json(GD.format(q=qq, m="artlist", s="1d") + "&maxrecords=100&sort=datedesc")
                arts = j.get("articles") or []
                ctx.save_raw(f"gdelt_{theme}.json", j)
                counts.append({"date": ctx.iso(), "source": "gdelt", "topic": theme, "items": len(arts)})
                for a in arts:
                    recs.append({"source": "gdelt", "topic": theme, "title": a.get("title"),
                                 "url": a.get("url"), "domain": a.get("domain"),
                                 "country": a.get("sourcecountry"), "seen": a.get("seendate")})
            except C.FetchError as e:
                fails.append(f"{theme}/artlist: {e}")
            C.pause(5.5)
        _store_news(ctx, t, recs, counts)
    _partial(t, fails, len(K.GDELT_THEMES) * (3 if ctx.mode == "nightly" else 2))


@source("social", modes=("nightly",))
def social(ctx, t):
    recs, counts, fails = [], [], []
    since = int(time.time()) - 86400
    for q in K.HN_QUERIES:
        try:
            j = C.fetch_json("https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=100"
                             f"&query={urllib.parse.quote(q)}&numericFilters=created_at_i>{since}")
            hits = j.get("hits") or []
            counts.append({"date": ctx.iso(), "source": "hackernews", "topic": q, "items": len(hits)})
            for h in hits:
                recs.append({"source": "hackernews", "topic": q, "title": h.get("title"),
                             "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                             "points": h.get("points"), "comments": h.get("num_comments"),
                             "seen": h.get("created_at")})
        except C.FetchError as e:
            fails.append(f"hn/{q}: {e}")
    for q in K.BLUESKY_QUERIES:
        try:
            j = C.fetch_json("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?sort=latest&limit=100"
                             f"&q={urllib.parse.quote(q)}")
            posts = j.get("posts") or []
            counts.append({"date": ctx.iso(), "source": "bluesky", "topic": q, "items": len(posts)})
            for p in posts:
                rec = p.get("record") or {}
                recs.append({"source": "bluesky", "topic": q, "title": (rec.get("text") or "")[:300],
                             "url": p.get("uri"), "author": (p.get("author") or {}).get("handle"),
                             "likes": p.get("likeCount"), "reposts": p.get("repostCount"),
                             "seen": rec.get("createdAt")})
        except C.FetchError as e:
            fails.append(f"bluesky/{q}: {e}")
    _store_news(ctx, t, recs, counts)
    _partial(t, fails, len(K.HN_QUERIES) + len(K.BLUESKY_QUERIES))


def _store_news(ctx, t, recs, counts):
    d = ctx.today
    path = os.path.join(ctx.bank, "news", f"{d.year:04d}", f"{d.month:02d}", f"{d.day:02d}.jsonl.gz")
    for r in recs:
        r["date"] = ctx.iso()
    a, _ = C.write_jsonl_gz(path, recs)
    t.added += a
    t.add(C.upsert_csv(ctx.table("news", "news_daily_counts.csv"), counts, key=("date", "source", "topic")))


# ====================================================================== SPACE
@source("space", modes=("nightly",))
def space(ctx, t):
    rows, fails = [], []
    for g in K.CELESTRAK_GROUPS:
        try:
            txt = C.fetch_text(f"https://celestrak.org/NORAD/elements/gp.php?GROUP={g}&FORMAT=tle", timeout=120)
            n = sum(1 for ln in txt.splitlines() if ln.startswith("1 "))
            if n == 0:
                raise C.FetchError(f"{g}: no TLEs in response")
            ctx.save_raw(f"tle_{g}.txt", txt)
            rows.append({"date": ctx.iso(), "group": g, "objects": n})
        except C.FetchError as e:
            fails.append(str(e))
        C.pause(1.0)
    if fails and any("stations" in f for f in fails):  # OPHANIM's own fallback mirror
        try:
            j = C.fetch_json("https://tle.ivanstanojevic.me/api/tle?page-size=100")
            ctx.save_raw("tle_mirror.json", j)
            rows.append({"date": ctx.iso(), "group": "mirror", "objects": len(j.get("member") or [])})
        except C.FetchError as e:
            fails.append(f"mirror: {e}")
    t.add(C.upsert_csv(ctx.table("geo", "space_daily.csv"), rows, key=("date", "group")))
    _partial(t, fails, len(K.CELESTRAK_GROUPS))


# ====================================================================== AIR
def _opensky_token(env):
    cid, sec = env.get("OPENSKY_CLIENT_ID"), env.get("OPENSKY_CLIENT_SECRET")
    if not (cid and sec):
        return None
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid,
                                   "client_secret": sec}).encode()
    j = json.loads(C.fetch("https://auth.opensky-network.org/auth/realms/opensky-network/protocol/"
                           "openid-connect/token", data=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded"}))
    return j.get("access_token")


@source("air", modes=("nightly",))
def air(ctx, t):
    fails = []
    d = ctx.iso()
    try:
        tok = _opensky_token(ctx.env)
        h = {"Authorization": f"Bearer {tok}"} if tok else None
        j = C.fetch_json("https://opensky-network.org/api/states/all", headers=h, timeout=90)
        states = j.get("states") or []
        ctx.save_raw("opensky_states.json", j)
        by_country = {}
        airborne = 0
        for s in states:
            by_country[s[2] or "?"] = by_country.get(s[2] or "?", 0) + 1
            airborne += 0 if s[8] else 1
        t.add(C.upsert_csv(ctx.table("geo", "air_daily.csv"), [{
            "date": d, "aircraft": len(states), "airborne": airborne}], key=("date",)))
        t.add(C.upsert_csv(ctx.table("geo", "air_by_country_daily.csv"),
                           [{"date": d, "country": c, "aircraft": n} for c, n in by_country.items()],
                           key=("date", "country")))
    except C.FetchError as e:
        fails.append(f"opensky: {e}")
    try:
        j = C.fetch_json("https://api.adsb.lol/v2/mil", timeout=90)
        ac = j.get("ac") or []
        ctx.save_raw("adsb_mil.json", j)
        t.add(C.upsert_csv(ctx.table("geo", "air_daily.csv"), [{"date": d, "military": len(ac)}],
                           key=("date",)))
    except C.FetchError as e:
        fails.append(f"adsb.lol: {e}")
    _partial(t, fails, 2)


# ====================================================================== SHIPS (aisstream)
def _in_box(lat, lon, box):
    a, b, c, e = box
    return a <= lat <= b and c <= lon <= e


AIS_URL = "wss://stream.aisstream.io/v0/stream"


async def _ais_sample(key, seconds, url=None):
    import websockets  # installed by the workflow
    latest = {}
    deadline = time.monotonic() + seconds
    async with websockets.connect(url or AIS_URL, open_timeout=30,
                                  max_size=2 ** 22) as ws:
        await ws.send(json.dumps({"APIKey": key, "BoundingBoxes": [[[-90, -180], [90, 180]]],
                                  "FilterMessageTypes": ["PositionReport"]}))
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.monotonic()))
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            m = json.loads(msg)
            md = m.get("MetaData") or {}
            pr = (m.get("Message") or {}).get("PositionReport") or {}
            if md.get("MMSI") is None:
                continue
            latest[md["MMSI"]] = (md.get("latitude"), md.get("longitude"), pr.get("Sog"),
                                  pr.get("NavigationalStatus"), (md.get("ShipName") or "").strip())
    return latest


def summarize_ships(latest, d):
    counts = {k: 0 for k in K.CHOKEPOINTS}
    anchored = {k: 0 for k in K.CHOKEPOINTS}
    for lat, lon, sog, nav, _ in latest.values():
        if lat is None or lon is None:
            continue
        for name, box in K.CHOKEPOINTS.items():
            if _in_box(lat, lon, box):
                counts[name] += 1
                if nav in (1, 5) or (sog is not None and sog < 0.5):
                    anchored[name] += 1
    rows = [{"date": d, "chokepoint": n, "vessels": counts[n], "stationary": anchored[n]} for n in counts]
    return rows


@source("ships", modes=("nightly",))
def ships(ctx, t):
    key = ctx.env.get("AISSTREAM_KEY", "").strip()
    if not key:
        raise C.FetchError("AISSTREAM_KEY secret not set — ships skipped. See databank/README.md")
    seconds = int(ctx.env.get("AIS_SECONDS", "300"))
    latest = asyncio.run(_ais_sample(key, seconds, ctx.env.get("AIS_URL")))
    if not latest:
        raise C.FetchError("aisstream returned no position reports")
    lines = ["mmsi,lat,lon,sog,nav_status,name"] + [
        f"{k},{v[0]},{v[1]},{v[2]},{v[3]},\"{v[4].replace(chr(34), '')}\"" for k, v in latest.items()]
    ctx.save_raw("ais_positions.csv", "\n".join(lines))
    d = ctx.iso()
    t.add(C.upsert_csv(ctx.table("geo", "ships_daily.csv"), [{
        "date": d, "vessels_seen": len(latest), "sample_seconds": seconds}], key=("date",)))
    t.add(C.upsert_csv(ctx.table("geo", "chokepoints_daily.csv"), summarize_ships(latest, d),
                       key=("date", "chokepoint")))


# ====================================================================== helpers
def _partial(t, fails, total, label=""):
    if not fails:
        return
    t.notes.extend(fails[:8])
    if len(fails) >= total:
        raise C.FetchError(f"{label + ': ' if label else ''}all {total} requests failed — {fails[0]}")
