"""DATA BANK harness — the quality gate. Every source runs against recorded-shape responses.

  python -m unittest tools.databank.test_databank -v
"""
import csv
import datetime as dt
import gzip
import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.parse

from tools.databank import common as C
from tools.databank import config as K
from tools.databank import run as R
from tools.databank import sources as S

TODAY = dt.date(2026, 10, 31)  # a Saturday → weekly SEC financials run; WB (Sunday) skips


def ts(d):
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp())


class Fake:
    """Routes URLs to payloads shaped exactly like each real API."""

    def __init__(self, today=TODAY, broken=()):
        self.today = today
        self.broken = set(broken)
        self.calls = []

    def __call__(self, url, headers=None, data=None, **kw):
        self.calls.append(url)
        for b in self.broken:
            if b in url:
                raise C.FetchError(f"{url} -> HTTP 503")
        J = lambda o: json.dumps(o).encode()
        td = self.today
        if "fredgraph.csv" in url:
            sid = url.split("id=")[1].split("&")[0]
            return (f"observation_date,{sid}\n1990-01-01,1.5\n1990-01-02,.\n"
                    f"{(td - dt.timedelta(days=1)).isoformat()},4.25\n").encode()
        if "daily-treasury-rates.csv" in url:
            y = int(url.split("daily-treasury-rates.csv/")[1].split("/")[0])
            return (f'Date,"1 Mo","3 Mo","2 Yr","10 Yr","30 Yr"\n'
                    f"12/31/{y},4.1,4.2,3.9,4.3,4.6\n01/02/{y},4.0,4.1,3.8,4.2,4.5\n").encode()
        if "debt_to_penny" in url:
            page = int(url.split("page[number]=")[1].split("&")[0])
            total = 2 if "page[size]=10000" in url else 1
            return J({"data": [{"record_date": f"199{page}-04-0{page}", "debt_held_public_amt": "3000000000000.12",
                                "intragov_hold_amt": "1.0", "tot_pub_debt_out_amt": "37500000000000.55"},
                               {"record_date": (td - dt.timedelta(days=1)).isoformat(),
                                "debt_held_public_amt": "1", "intragov_hold_amt": "1",
                                "tot_pub_debt_out_amt": "38000000000000"}],
                      "meta": {"total-pages": total}})
        if "avg_interest_rates" in url:
            return J({"data": [{"record_date": "2001-01-31", "security_type_desc": "Marketable",
                                "security_desc": "Treasury Bills", "avg_interest_rate_amt": "5.95"}],
                      "meta": {"total-pages": 1}})
        if "frankfurter" in url:
            a = url.split("/")[-1].split("..")[0]
            return J({"base": "USD", "rates": {a: {"EUR": 0.92, "JPY": 150.1, "GBP": 0.79}}})
        if "api.worldbank.org" in url:
            page = int(url.rsplit("&page=", 1)[1])
            return J([{"page": page, "pages": 2},
                      [{"countryiso3code": "USA", "country": {"id": "US", "value": "United States"},
                        "date": str(1960 + page), "value": 543.3},
                       {"countryiso3code": "MEX", "country": {"id": "MX", "value": "Mexico"},
                        "date": str(1960 + page), "value": None}]])
        if "data.sec.gov/submissions" in url:
            assert headers and "User-Agent" in headers
            return J({"filings": {"recent": {"accessionNumber": ["0001-26-000001", "0001-26-000002"],
                                             "filingDate": ["2026-10-20", "2026-10-29"],
                                             "reportDate": ["2026-09-30", ""], "form": ["10-Q", "8-K"],
                                             "primaryDocument": ["a.htm", "b.htm"],
                                             "primaryDocDescription": ["10-Q", "8-K"]}}})
        if "companyfacts" in url:
            return J({"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": [
                {"start": "2009-01-01", "end": "2009-12-31", "val": 1e9, "accn": "x1", "fy": 2009, "fp": "FY",
                 "form": "10-K", "filed": "2010-02-20"},
                {"start": "2025-01-01", "end": "2025-12-31", "val": 5e10, "accn": "x2", "fy": 2025, "fp": "FY",
                 "form": "10-K", "filed": "2026-02-20", "frame": "CY2025"}]}}},
                "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                    {"end": "2026-09-30", "val": 2.8e9, "accn": "x3", "fy": 2026, "fp": "Q3", "form": "10-Q",
                     "filed": "2026-10-30"}]}}}}})
        if "api.exchange.coinbase.com" in url:
            start = dt.date.fromisoformat(url.split("start=")[1].split("&")[0])
            end = dt.date.fromisoformat(url.split("end=")[1].split("&")[0])
            listed = dt.date(2015, 1, 1)
            out, d = [], end
            while d >= max(start, listed):  # real API returns only candles that exist
                out.append([ts(d), 1.0, 3.0, 2.0, 2.5, 100.0])  # newest first, like Coinbase
                d -= dt.timedelta(days=1)
            return J(out)
        if "coins/markets" in url:
            return J([{"id": "bitcoin", "symbol": "btc", "market_cap_rank": 1, "current_price": 110000,
                       "market_cap": 2.2e12, "total_volume": 4e10, "price_change_percentage_24h": 1.2,
                       "price_change_percentage_7d_in_currency": 3.4, "circulating_supply": 19.9e6}])
        if "coingecko.com/api/v3/global" in url:
            return J({"data": {"total_market_cap": {"usd": 3.9e12}, "total_volume": {"usd": 1.5e11},
                               "market_cap_percentage": {"btc": 57.1, "eth": 11.2}, "active_cryptocurrencies": 17000}})
        if "mempool.space/api/v1/mining/hashrate/all" in url:
            return J({"hashrates": [{"timestamp": 1231459200, "avgHashrate": 7e6}],
                      "difficulty": [{"time": 1231459200, "height": 0, "difficulty": 1, "adjustment": 1}]})
        if "fees/recommended" in url:
            return J({"fastestFee": 5, "halfHourFee": 4, "hourFee": 3, "economyFee": 2, "minimumFee": 1})
        if url.endswith("/api/mempool"):
            return J({"count": 12000, "vsize": 9000000, "total_fee": 15000000, "fee_histogram": []})
        if "blocks/tip/height" in url:
            return b"920000"
        if "mempool.space/api/v1/blocks/" in url:
            h = int(url.rsplit("/", 1)[1])
            return J([{"height": h - i, "timestamp": ts(self.today) - i * 600, "tx_count": 3000,
                       "size": 1.5e6, "weight": 4e6, "extras": {"totalFees": 1e7, "medianFee": 3,
                                                                 "reward": 3.2e8, "pool": {"name": "Foundry"}}}
                      for i in range(15)])
        if "query1.finance.yahoo.com" in url:
            sym = urllib.parse.unquote(url.split("/chart/")[1].split("?")[0])
            if sym in ("SI=F", "^STOXX50E"):
                raise C.FetchError(f"{url} -> HTTP 404")
            first = dt.date(1927, 12, 30) if "period1" in url else td - dt.timedelta(days=5)
            days = [first, td - dt.timedelta(days=2), td]
            return J({"chart": {"result": [{
                "meta": {"symbol": sym, "gmtoffset": -18000},
                "timestamp": [ts(d) + 14 * 3600 + 30 * 60 for d in days],
                "indicators": {"quote": [{"open": [17.6, 6800, 6900], "high": [17.7, 6850, 6950],
                                          "low": [17.5, 6790, 6880], "close": [17.66, 6840, None],
                                          "volume": [0, 1, 2]}],
                               "adjclose": [{"adjclose": [17.66, 6840, None]}]}}], "error": None}})
        if "stooq.com" in url:
            if "silver" in url or "xagusd" in url:
                return b"No data"
            return b"Date,Open,High,Low,Close,Volume\n1789-05-01,0.5,0.5,0.5,0.5,0\n2026-10-29,6800,6850,6790,6840,1\n"
        if "gdeltproject" in url:
            if "timeline" in url:
                return J({"timeline": [{"series": "x", "data": [
                    {"date": (td - dt.timedelta(days=2)).strftime("%Y%m%dT000000Z"), "value": -1.0},
                    {"date": (td - dt.timedelta(days=2)).strftime("%Y%m%dT120000Z"), "value": -2.0},
                    {"date": td.strftime("%Y%m%dT010000Z"), "value": -9.0}]}]})
            theme = url.split("query=")[1][:12]
            return J({"articles": [{"url": f"https://news.example/{theme}/{i}", "title": f"Headline {i}",
                                    "domain": "news.example", "sourcecountry": "United States",
                                    "seendate": "20261031T050000Z"} for i in range(3)]
                      + [{"url": "https://news.example/shared", "title": "Cross-theme story"}]})
        if "hn.algolia.com" in url:
            return J({"hits": [{"title": "Fed holds", "url": "https://hn.example/1", "points": 50,
                                "num_comments": 10, "created_at": "2026-10-31T01:00:00Z", "objectID": "1"}]})
        if "bsky.app" in url:
            return J({"posts": [{"uri": "at://did:x/post/1", "author": {"handle": "a.bsky.social"},
                                 "record": {"text": "markets", "createdAt": "2026-10-31T01:00:00Z"},
                                 "likeCount": 3, "repostCount": 1}]})
        if "celestrak.org" in url:
            return b"ISS (ZARYA)\n1 25544U 98067A   26303.5 .00016717 00000-0 10270-3 0  9994\n2 25544  51.6416\n"
        if "opensky-network.org" in url:
            return J({"time": 1, "states": [["abc", "UAL1", "United States", 0, 0, -95.3, 29.9, 10000, False],
                                            ["def", "BAW2", "United Kingdom", 0, 0, -0.4, 51.4, 0, True]]})
        if "adsb.lol/v2/mil" in url:
            return J({"ac": [{"hex": "ae0001"}, {"hex": "ae0002"}], "total": 2})
        raise C.FetchError(f"unrouted fixture URL: {url}")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bank = os.path.join(self.dir, "databank")
        self.raw = os.path.join(self.dir, "raw")
        self._fetch, self._sleep = C.fetch_impl, C.sleep_impl
        C.sleep_impl = lambda s: None
        self.env = {"SEC_USER_AGENT": "Test Co test@example.com"}

    def tearDown(self):
        C.fetch_impl, C.sleep_impl = self._fetch, self._sleep
        shutil.rmtree(self.dir)

    def go(self, mode, fake=None, today=TODAY, only=None, env=None):
        C.fetch_impl = fake or Fake(today)
        return R.run(mode, self.bank, self.raw, only=only, today=today,
                     env=self.env if env is None else env)

    def manifest(self):
        with open(os.path.join(self.bank, "manifest.json")) as f:
            return json.load(f)

    def rows(self, *parts):
        with open(os.path.join(self.bank, "tables", *parts), newline="") as f:
            return list(csv.DictReader(f))


class TestTables(Base):
    def test_upsert_never_deletes_and_takes_revisions(self):
        p = os.path.join(self.dir, "t.csv")
        self.assertEqual(C.upsert_csv(p, [{"date": "2026-01-01", "v": 1}, {"date": "2026-01-02", "v": 2}],
                                      key=("date",)), (2, 0, 2))
        self.assertEqual(C.upsert_csv(p, [{"date": "2026-01-02", "v": 2}], key=("date",)), (0, 0, 2))
        self.assertEqual(C.upsert_csv(p, [{"date": "2026-01-02", "v": 2.5}], key=("date",)), (0, 1, 2))
        self.assertEqual(C.upsert_csv(p, [], key=("date",)), (0, 0, 2))
        with open(p) as f:
            self.assertEqual(f.read(), "date,v\n2026-01-01,1\n2026-01-02,2.5\n")

    def test_numeric_keys_sort_numerically_and_big_numbers_survive(self):
        p = os.path.join(self.dir, "b.csv")
        C.upsert_csv(p, [{"height": 10, "v": 38000000000000.55}, {"height": 9, "v": 1}], key=("height",))
        with open(p) as f:
            self.assertEqual(f.read().splitlines()[1:], ["9,1", "10,38000000000000.55"])

    def test_num_parsing(self):
        self.assertIsNone(C.num("."))
        self.assertIsNone(C.num(""))
        self.assertEqual(C.num("4.25"), 4.25)
        self.assertEqual(C.num("37500000000000.55"), 37500000000000.55)
        self.assertEqual(C.num("920000"), 920000)


class TestNightly(Base):
    def test_every_source_green_and_bank_populated(self):
        env = dict(self.env)  # no AIS key → ships reports a clear failure, nothing else affected
        rep = self.go("nightly", env=env)
        status = {r["source"]: r for r in rep["sources"]}
        for name in ("fred", "treasury_yields", "treasury_debt", "fx_ecb", "worldbank", "sec_filings",
                     "sec_financials", "crypto_coinbase", "crypto_coingecko", "bitcoin_chain",
                     "markets", "gdelt", "social", "space", "air"):
            self.assertTrue(status[name]["ok"], f"{name}: {status[name]['error']}")
        self.assertFalse(status["ships"]["ok"])
        self.assertIn("AISSTREAM_KEY", status["ships"]["error"])
        self.assertIn("weekly", " ".join(status["worldbank"]["notes"]))
        # silver: Yahoo 404 AND Stooq "No data" → noted, not fatal; Euro Stoxx has no fallback → noted
        notes = " ".join(status["markets"]["notes"])
        self.assertIn("SILVER", notes)
        self.assertIn("EUROSTOXX50", notes)

        m = self.manifest()
        paths = {t["path"] for t in m["tables"]}
        for must in ("tables/macro/fred/DGS10.csv", "tables/rates/treasury/yield_curve_nominal.csv",
                     "tables/rates/treasury/debt_to_penny.csv", "tables/fx/ecb_per_usd.csv",
                     "tables/companies/sec_filings.csv", "tables/companies/sec_financials/jpm.csv",
                     "tables/crypto/coinbase/BTC-USD.csv", "tables/crypto/coingecko_top100_daily.csv",
                     "tables/crypto/btc_blocks.csv", "tables/crypto/btc_daily.csv",
                     "tables/markets/daily/SPX.csv", "tables/news/gdelt_daily.csv",
                     "tables/news/news_daily_counts.csv", "tables/geo/space_daily.csv",
                     "tables/geo/air_daily.csv", "tables/geo/air_by_country_daily.csv"):
            self.assertIn(must, paths)
        self.assertEqual(m["news_days"], 1)

        # FRED '.' gaps dropped
        self.assertEqual(len(self.rows("macro", "fred", "DGS10.csv")), 2)
        # treasury CSV dates normalized to ISO
        self.assertEqual(self.rows("rates", "treasury", "yield_curve_nominal.csv")[0]["date"], "2026-01-02")
        # no still-forming candle for today
        self.assertTrue(all(r["date"] < TODAY.isoformat() for r in self.rows("crypto", "coinbase", "BTC-USD.csv")))
        # GDELT: completed days only, intraday points averaged
        g = self.rows("news", "gdelt_daily.csv")
        self.assertTrue(all(r["date"] < TODAY.isoformat() for r in g))
        self.assertEqual(float(g[0]["tone"]), -1.5)
        # air: both sources merged into one daily row
        a = self.rows("geo", "air_daily.csv")[0]
        self.assertEqual((a["aircraft"], a["airborne"], a["military"]), ("2", "1", "2"))
        # ~165 blocks pulled for the day
        self.assertGreaterEqual(len(self.rows("crypto", "btc_blocks.csv")), 144)
        # news deduped across themes by URL
        with gzip.open(os.path.join(self.bank, "news", "2026", "10", "31.jsonl.gz"), "rt") as f:
            recs = [json.loads(x) for x in f]
        self.assertEqual(sum(1 for r in recs if r["url"] == "https://news.example/shared"), 1)
        # raw archive written
        self.assertTrue(os.path.exists(os.path.join(self.raw, "opensky_states.json.gz")))
        self.assertTrue(os.path.exists(os.path.join(self.raw, "tle_active.txt.gz")))

    def test_same_night_twice_adds_nothing(self):
        self.go("nightly")
        before = self.manifest()["total_rows"]
        rep = self.go("nightly")
        self.assertEqual(sum(r["added"] for r in rep["sources"]), 0)
        after = self.manifest()["total_rows"]
        self.assertEqual(before, after)

    def test_compounds_day_over_day(self):
        self.go("nightly")
        n1 = len(self.rows("crypto", "coingecko_top100_daily.csv"))
        d1 = len(self.rows("crypto", "btc_daily.csv"))
        nxt = TODAY + dt.timedelta(days=1)
        self.go("nightly", fake=Fake(nxt), today=nxt)
        self.assertEqual(len(self.rows("crypto", "coingecko_top100_daily.csv")), n1 + 1)
        self.assertEqual(len(self.rows("crypto", "btc_daily.csv")), d1 + 1)
        self.assertEqual(self.manifest()["news_days"], 2)
        with open(os.path.join(self.bank, "health", "runs.csv")) as f:
            runs = list(csv.DictReader(f))
        self.assertEqual({r["date"] for r in runs}, {TODAY.isoformat(), nxt.isoformat()})

    def test_one_dead_source_never_stops_the_rest(self):
        rep = self.go("nightly", fake=Fake(broken=("mempool.space", "coingecko")))
        st = {r["source"]: r["ok"] for r in rep["sources"]}
        self.assertFalse(st["bitcoin_chain"])
        self.assertFalse(st["crypto_coingecko"])
        self.assertTrue(st["fred"] and st["gdelt"] and st["crypto_coinbase"])
        self.assertEqual(R.main.__name__, "main")

    def test_everything_dead_fails_the_job(self):
        C.sleep_impl = lambda s: None

        def dead(url, **kw):
            raise C.FetchError("down")
        C.fetch_impl = dead
        self.assertEqual(R.main(["--mode", "nightly", "--bank", self.bank]), 1)

    def test_partial_fred_outage_keeps_good_series(self):
        rep = self.go("nightly", fake=Fake(broken=("id=DGS10",)), only={"fred"})
        r = rep["sources"][0]
        self.assertTrue(r["ok"])
        self.assertTrue(any("DGS10" in n for n in r["notes"]))
        self.assertFalse(os.path.exists(os.path.join(self.bank, "tables", "macro", "fred", "DGS10.csv")))
        self.assertTrue(os.path.exists(os.path.join(self.bank, "tables", "macro", "fred", "DFF.csv")))

    def test_sec_requires_contact(self):
        rep = self.go("nightly", env={}, only={"sec_filings"})
        self.assertIn("SEC_USER_AGENT", rep["sources"][0]["error"])


class TestBackfill(Base):
    def test_deep_history(self):
        rep = self.go("backfill")
        st = {r["source"]: r for r in rep["sources"]}
        bad = {k: v["error"] for k, v in st.items() if not v["ok"]}
        self.assertEqual(bad, {})
        # nightly-only sources never run in backfill
        self.assertNotIn("air", st)
        self.assertNotIn("ships", st)
        # FRED full history (no cosd)
        self.assertEqual(self.rows("macro", "fred", "UNRATE.csv")[0]["date"], "1990-01-01")
        # Treasury nominal curve every year 1990 → now; real from 2003
        nom = self.rows("rates", "treasury", "yield_curve_nominal.csv")
        self.assertEqual(nom[0]["date"], "1990-01-02")
        self.assertEqual(len(nom), 2 * (TODAY.year - 1990 + 1))
        self.assertEqual(self.rows("rates", "treasury", "yield_curve_real.csv")[0]["date"], "2003-01-02")
        # FiscalData paginates
        self.assertEqual(self.rows("rates", "treasury", "debt_to_penny.csv")[0]["date"], "1991-04-01")
        # FX one call per year since 1999
        fx = self.rows("fx", "ecb_per_usd.csv")
        self.assertEqual(fx[0]["date"], "1999-01-04")
        self.assertEqual(len(fx), TODAY.year - 1999 + 1)
        # World Bank: runs in backfill regardless of weekday, paginates, drops nulls
        wb = self.rows("macro", "worldbank", "NY.GDP.MKTP.CD.csv")
        self.assertEqual([r["year"] for r in wb], ["1961", "1962"])
        # Coinbase walks back to listing date and stops
        btc = self.rows("crypto", "coinbase", "BTC-USD.csv")
        self.assertEqual(btc[0]["date"], "2015-01-01")
        self.assertEqual(btc[-1]["date"], (TODAY - dt.timedelta(days=1)).isoformat())
        self.assertEqual(len(btc), (TODAY - dt.date(2015, 1, 1)).days)
        # SEC: every period on file, plus shares outstanding
        jpm = self.rows("companies", "sec_financials", "jpm.csv")
        self.assertEqual({r["concept"] for r in jpm}, {"NetIncomeLoss", "EntityCommonStockSharesOutstanding"})
        self.assertEqual(min(r["end"] for r in jpm), "2009-12-31")
        # Bitcoin hashrate back to genesis
        self.assertEqual(self.rows("crypto", "btc_hashrate.csv")[0]["date"], "2009-01-09")
        # Yahoo long index history, exchange-local dates, no forming session, adj close kept
        spx = self.rows("markets", "daily", "SPX.csv")
        self.assertEqual(spx[0]["date"], "1927-12-30")
        self.assertEqual(spx[0]["src"], "yahoo")
        self.assertEqual(spx[0]["adj_close"], "17.66")
        self.assertTrue(all(r["date"] < TODAY.isoformat() for r in spx))

    def test_backfill_then_nightly_extends_same_tables(self):
        self.go("backfill", only={"crypto_coinbase", "fx_ecb"})
        n = len(self.rows("crypto", "coinbase", "BTC-USD.csv"))
        nxt = TODAY + dt.timedelta(days=1)
        self.go("nightly", fake=Fake(nxt), today=nxt, only={"crypto_coinbase", "fx_ecb"})
        self.assertEqual(len(self.rows("crypto", "coinbase", "BTC-USD.csv")), n + 1)

    def test_backfill_is_safe_to_rerun(self):
        self.go("backfill", only={"fred", "treasury_yields"})
        rep = self.go("backfill", only={"fred", "treasury_yields"})
        self.assertEqual(sum(r["added"] + r["updated"] for r in rep["sources"]), 0)


class TestMarketsAndGdelt(Base):
    def test_stooq_fallback_when_yahoo_fails(self):
        f = Fake(broken=("query1.finance.yahoo.com/v8/finance/chart/%5EGSPC",))
        rep = self.go("nightly", fake=f, only={"markets"})
        self.assertTrue(rep["sources"][0]["ok"])
        spx = self.rows("markets", "daily", "SPX.csv")
        self.assertEqual({r["src"] for r in spx}, {"stooq"})
        self.assertEqual(self.rows("markets", "daily", "DJI.csv")[0]["src"], "yahoo")

    def test_gdelt_waits_out_429_then_succeeds(self):
        base, hits, waits = Fake(), {"n": 0}, []

        def flaky(url, **kw):
            if "gdeltproject" in url and hits["n"] < 2:
                hits["n"] += 1
                raise C.FetchError(f"{url} -> HTTP 429")
            return base(url, **kw)
        C.fetch_impl = flaky
        C.sleep_impl = waits.append
        rep = R.run("backfill", self.bank, None, only={"gdelt"}, today=TODAY, env=self.env)
        r = rep["sources"][0]
        self.assertTrue(r["ok"], r["error"])
        self.assertEqual(r["notes"], [])
        self.assertIn(30, waits)
        self.assertIn(90, waits)
        self.assertEqual({x["theme"] for x in self.rows("news", "gdelt_daily.csv")}, set(K.GDELT_THEMES))

    def test_gdelt_empty_or_text_answers_are_reported_not_silent(self):
        def empty(url, **kw):
            if "gdeltproject" in url:
                return b"{}" if "timelinetone" in url else b"Please limit requests to one every 5 seconds"
            raise C.FetchError("unrouted")
        C.fetch_impl = empty
        rep = R.run("backfill", self.bank, None, only={"gdelt"}, today=TODAY, env=self.env)
        r = rep["sources"][0]
        self.assertFalse(r["ok"])  # every request came back useless → loud failure
        self.assertIn("empty timeline", " ".join(r["notes"]))


class TestGdeltCircuitBreaker(Base):
    def test_blocked_runner_stops_fast_and_runs_last(self):
        base, waits, gd = Fake(), [], {"n": 0}

        def blocked(url, **kw):
            if "gdeltproject" in url:
                gd["n"] += 1
                raise C.FetchError(f"{url} -> HTTP 429")
            return base(url, **kw)
        C.fetch_impl = blocked
        C.sleep_impl = waits.append
        rep = R.run("nightly", self.bank, None, today=TODAY, env=self.env)
        names = [r["source"] for r in rep["sources"]]
        self.assertEqual(names[-1], "gdelt")
        g = rep["sources"][-1]
        self.assertFalse(g["ok"])
        self.assertIn("refused this runner", g["notes"][0])
        # nightly: 2 blocked requests x 2 attempts each (one 30s wait) — then stop
        self.assertEqual(gd["n"], 4)
        self.assertEqual(sum(1 for w in waits if w == 30), 2)
        self.assertNotIn(90, waits)
        self.assertTrue(all(r["ok"] for r in rep["sources"] if r["source"] in ("fred", "air", "space")))


class TestShips(Base):
    def test_chokepoint_counts(self):
        latest = {1: (26.5, 56.3, 12.0, 0, "TANKER A"),   # Hormuz, moving
                  2: (26.6, 56.1, 0.1, 1, "TANKER B"),    # Hormuz, at anchor
                  3: (30.5, 32.4, 8.0, 0, "BOX C"),       # Suez
                  4: (0.0, 0.0, 5.0, 0, "NOWHERE"),
                  5: (None, None, None, None, "")}
        rows = {r["chokepoint"]: r for r in S.summarize_ships(latest, "2026-10-31")}
        self.assertEqual((rows["hormuz"]["vessels"], rows["hormuz"]["stationary"]), (2, 1))
        self.assertEqual(rows["suez"]["vessels"], 1)
        self.assertEqual(rows["panama"]["vessels"], 0)
        self.assertEqual(set(rows), set(K.CHOKEPOINTS))


class TestShipsLive(Base):
    """Runs the real websocket client against a local fake aisstream server."""

    def test_stream_to_bank(self):
        try:
            import asyncio
            import threading
            import websockets
            from websockets.asyncio.server import serve
        except ImportError:
            self.skipTest("websockets not installed")
        got = {}

        async def handler(ws):
            got["sub"] = json.loads(await ws.recv())
            msgs = [(111, 26.5, 56.3, 12.0, 0), (222, 26.6, 56.1, 0.0, 1), (333, 30.5, 32.4, 8.0, 0),
                    (111, 26.7, 56.5, 11.0, 0)]  # 111 reported twice → counted once, latest position kept
            for mmsi, lat, lon, sog, nav in msgs:
                await ws.send(json.dumps({"MessageType": "PositionReport",
                                          "MetaData": {"MMSI": mmsi, "latitude": lat, "longitude": lon,
                                                       "ShipName": f"V{mmsi}"},
                                          "Message": {"PositionReport": {"Sog": sog, "NavigationalStatus": nav}}}))
            await ws.close()

        ready, port = threading.Event(), {}

        def server():
            async def main():
                async with serve(handler, "127.0.0.1", 0) as srv:
                    port["p"] = srv.sockets[0].getsockname()[1]
                    ready.set()
                    await asyncio.sleep(5)
            asyncio.run(main())

        th = threading.Thread(target=server, daemon=True)
        th.start()
        ready.wait(5)
        env = dict(self.env, AISSTREAM_KEY="k123", AIS_SECONDS="3", AIS_URL=f"ws://127.0.0.1:{port['p']}")
        rep = self.go("nightly", env=env, only={"ships"})
        self.assertTrue(rep["sources"][0]["ok"], rep["sources"][0]["error"])
        self.assertEqual(got["sub"]["APIKey"], "k123")
        self.assertEqual(self.rows("geo", "ships_daily.csv")[0]["vessels_seen"], "3")
        cp = {r["chokepoint"]: r for r in self.rows("geo", "chokepoints_daily.csv")}
        self.assertEqual((cp["hormuz"]["vessels"], cp["hormuz"]["stationary"]), ("2", "1"))
        self.assertEqual(cp["suez"]["vessels"], "1")
        self.assertTrue(os.path.exists(os.path.join(self.raw, "ais_positions.csv.gz")))


class TestWorkflows(unittest.TestCase):
    def test_workflows_are_valid_yaml_with_required_pieces(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml not installed")
        root = os.path.join(os.path.dirname(__file__), "..", "..", ".github", "workflows")
        for fn, must in (("databank-nightly.yml", ["schedule", "release", "--mode nightly"]),
                         ("databank-backfill.yml", ["workflow_dispatch", "--mode backfill"])):
            with open(os.path.join(root, fn)) as f:
                text = f.read()
            wf = yaml.safe_load(text)
            self.assertIn("jobs", wf)
            self.assertEqual(wf["permissions"]["contents"], "write")
            for m in must:
                self.assertIn(m, text, f"{fn} missing {m}")


if __name__ == "__main__":
    unittest.main()
