# OPHANIM — Global Financial Digital Twin (v3.5)

**The Wheel of Eyes.** A single-file command center for the world financial system:
10 modes · 111-chart wall · SYNAPSE (Medallion signal audit + Aladdin Monte Carlo VaR veto) ·
AUTOGNOSIS self-learning weights · CHOKEPOINT WATCH · SETTLEMENT WATCH · DATACENTER WATCH ·
OMNIFEED universal data connector · WARWATCH theater pulses · 3D Earth with an OVERWATCH
panel that launches **God's Eye View**.

## Run it
This is one HTML file. Open `index.html` — or serve it with GitHub Pages:
**Settings → Pages → Deploy from a branch → main / (root) → Save.**
Your live URL: `https://<username>.github.io/<repo>/`

## God's Eye View
OPHANIM's built-in God's Eye was retired in v3.5 in favour of Bilawal Sidhu's open-source
[God's Eye View](https://github.com/bilawalsidhu/gods-eye-view) (MIT): photorealistic 3D
globe, live aircraft, ships, satellites, rail, cameras, cockpit view and voice control.
It needs a small server, so it runs on your own PC through
[Pinokio](https://pinokio.co/apps/github-com-bilawalsidhu-gods-eye-view) and updates itself.
In OPHANIM, open the 3D Earth → OVERWATCH → ⚙ once to paste the address Pinokio shows,
then 🌐 GOD'S EYE VIEW opens it at whatever spot you're looking at.
Chokepoint Watch keeps its own live ship feed: OVERWATCH → 🚢 AIS FEED (free aisstream.io key).

The previous version is kept at [`archive/ophanim-v2.9.html`](archive/ophanim-v2.9.html).
Verify any change with `npm i jsdom && node tools/ophanim/harness.js` (must print ALL GREEN).

## Live data sources
CoinGecko · Coinbase WS · ECB/Frankfurter · GDELT · World Bank · SEC EDGAR ·
Wikipedia · mempool.space · US Treasury FiscalData · Bluesky · Hacker News ·
Celestrak (SGP4) · OpenSky/adsb.lol · Esri/OSM/Carto imagery.
Optional free keys (prompt-entered, memory-only, never stored): CoinGecko demo ·
aisstream.io (Chokepoint Watch AIS feed, remembered in this browser) · Anthropic (cloud AI analyst).

## Data Bank — permanent memory
Every night a GitHub Action snapshots every feed into [`databank/`](databank/README.md),
compounding forever. A one-time backfill seeds it with history back to 1990 for the
Treasury yield curve, 1960 for world economies, and earlier for US macro and market
indices. See [databank/README.md](databank/README.md) for coverage and the two-minute setup.

*Paper trading only. SIM-badged series are indicative, not market data. Not investment advice.*
