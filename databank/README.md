# OPHANIM DATA BANK

OPHANIM's permanent memory. Every night it snapshots every feed and compounds it here. Rows are only ever added or corrected, never deleted. A one-time backfill seeds the bank with the deepest history each source offers.

## Layout

| Path | What it holds | Growth |
|---|---|---|
| `tables/**.csv` | Living time-series tables. The backfill seeds them, and every night extends them. | ~35 MB one-time, then ~12 MB/yr |
| `news/YYYY/MM/DD.jsonl.gz` | Every headline and post collected that day (GDELT, Hacker News, Bluesky), deduped | ~30 MB/yr |
| `manifest.json` | Index of every table: rows, columns, first and last date | rewritten nightly |
| `health/` | Per-source status for every run (`latest-nightly.json`, `runs.csv`) | tiny |
| GitHub **Releases** `databank-raw-YYYY-MM` | Full untouched nightly payloads: every aircraft, ship, satellite element set and raw API response | ~2 MB/night, stored outside the repo |

The heavy raw geospatial data goes to Releases, so the repo stays small enough to last decades.

## Coverage — how far back each table goes

| Domain | Table | Source | History starts |
|---|---|---|---|
| US recessions | `macro/fred/USREC.csv` | FRED | 1854 |
| Industrial production | `macro/fred/INDPRO.csv` | FRED | 1919 |
| Payrolls, CPI, GDP, unemployment | `macro/fred/*.csv` | FRED | 1939–1948 |
| Fed funds, M2, 10Y yield | `macro/fred/*.csv` | FRED | 1954–1962 |
| S&P 500, Dow, Nasdaq, Russell, Nikkei, DAX, FTSE, Hang Seng, Euro Stoxx, dollar index, gold, silver, oil, copper | `markets/daily/*.csv` | Yahoo Finance (Stooq fallback) | S&P from 1927; others from their listing |
| World economies, 15 indicators × ~260 countries | `macro/worldbank/*.csv` | World Bank | 1960 |
| VIX, oil (WTI/Brent) | `macro/fred/*.csv` | FRED | 1986–1990 |
| Full Treasury yield curve, daily | `rates/treasury/yield_curve_nominal.csv` | US Treasury | **1990** |
| National debt, daily | `rates/treasury/debt_to_penny.csv` | FiscalData | 1993 |
| FX, ~30 currencies per USD, daily | `fx/ecb_per_usd.csv` | ECB via Frankfurter | 1999 |
| Average rates on US debt | `rates/treasury/avg_interest_rates.csv` | FiscalData | 2001 |
| Real (TIPS) yield curve | `rates/treasury/yield_curve_real.csv` | US Treasury | 2003 |
| Financials of 28 institutions in OPHANIM's twin | `companies/sec_financials/*.csv` | SEC EDGAR XBRL | ~2009 |
| Bitcoin hashrate and difficulty | `crypto/btc_hashrate.csv` | mempool.space | 2009 |
| 12 crypto pairs, daily OHLCV | `crypto/coinbase/*.csv` | Coinbase | 2015 (from listing) |
| **Global events: tone, conflict vs. cooperation, protests, violence — worldwide and per country, daily** | `news/gdelt_events/world_daily.csv`, `news/gdelt_events/by_country/YYYY-MM.csv` | GDELT raw event files | **1979** (fills backward from today, about 5 hours of backfill runs) |
| News tone and volume, 8 themes | `news/gdelt_daily.csv` | GDELT search service (often blocks GitHub) | best effort |

**Starts on day one and compounds forward** (no free archive exists): CoinGecko top 100 coins, Bitcoin blocks and fees, aircraft counts by country, military aircraft, ship counts at 9 chokepoints, satellite catalog, headlines and social posts.

## Setup (one time)

In the repo: **Settings → Secrets and variables → Actions**

| Name | Type | Needed for | Value |
|---|---|---|---|
| `SEC_USER_AGENT` | **Variable** | SEC filings and financials | Your name and email, e.g. `Millenia Ecommerce LLC you@example.com`. The SEC requires a contact. |
| `AISSTREAM_KEY` | Secret | Ships and chokepoints | Same aisstream.io key you use in OPHANIM |
| `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET` | Secret | Optional: more reliable aircraft data | From your OpenSky account |

Then go to **Actions → "OPHANIM Data Bank · Backfill history" → Run workflow**. It takes roughly 20–40 minutes. The nightly job starts on its own at about 1:17 AM Houston time.

Any source without its key reports `FAIL` in `health/` and everything else keeps running.

## Rules the engine follows

- **Never deletes.** New rows are appended, and source revisions update values in place. The raw archive keeps the original versions.
- **Self-healing.** Each night re-pulls an overlapping recent window, so a missed night fills itself in.
- **Isolated sources.** One dead API never stops the others. The job only fails if every source fails.
- **Only completed periods.** Today's still-forming candle or news day is never stored as final.
- **Harness-gated.** `python -m unittest tools.databank.test_databank` must pass green. CI runs it on every change.

## Notes

- This repo is public (GitHub Pages), so the bank is publicly browsable. Every source is public data. To make it private, move `databank/` and the workflows to a private repo.
- GitHub disables scheduled workflows after 60 days with no repo activity. The nightly commit counts as activity, so it keeps itself alive.
- Add a feed by putting a function in `tools/databank/sources.py` with `@source("name")`, then adding a fixture and a test.
