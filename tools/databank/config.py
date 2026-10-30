"""What the bank tracks. Lists mirror OPHANIM index.html so the bank and the site stay in sync."""

# Coinbase pairs OPHANIM streams live (index.html PRODUCTS)
CRYPTO_PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "ADA-USD",
                   "AVAX-USD", "LINK-USD", "LTC-USD", "DOT-USD", "UNI-USD", "BCH-USD"]

# SEC CIK map — the institutions in OPHANIM's twin (index.html PanelIntel.CIK)
CIKS = {"jpm": 19617, "gs": 886982, "ms": 895421, "citi": 831001, "bofa": 70858,
        "blackrock": 1364742, "visa": 1403161, "mastercard": 1141391, "amex": 4962,
        "paypal": 1633917, "coinbase": 1679788, "ice": 858339, "cme": 1156375,
        "nasdaq": 1120193, "msci": 1408198, "spglobal": 64040, "moodys": 1059556,
        "schwab": 316709, "ibkr": 1381197, "robinhood": 1783879, "statestreet": 93751,
        "ntrs": 73124, "bnym": 1390777, "berkshire": 1067983, "aig": 5272,
        "factset": 1013237, "blackstone": 1393818, "kkr": 1404912}

# XBRL concepts pulled for every company, every period on file
SEC_CONCEPTS = [
    "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenuesNetOfInterestExpense", "NetIncomeLoss", "OperatingIncomeLoss",
    "Assets", "Liabilities", "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue", "LongTermDebt",
    "EarningsPerShareDiluted", "EarningsPerShareBasic",
    "InterestExpense", "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsForRepurchaseOfCommonStock", "PaymentsOfDividends",
]
SEC_DEI = ["EntityCommonStockSharesOutstanding"]

# GDELT themes (index.html Data Hub NEWS WIRE THEMES) + the OMNIFEED wire query
GDELT_THEMES = {"markets": '"stock market"', "banking": '"central bank"',
                "crypto": "cryptocurrency", "regulation": '"financial regulation"',
                "energy": '"oil prices"', "economy": "inflation",
                "tokenization": "tokenization", "shipping": '"shipping"'}

# World Bank: OPHANIM's 6 dossier indicators + macro context for pattern work
WB_INDICATORS = {
    "NY.GDP.MKTP.CD": "GDP (current US$)",
    "NY.GDP.MKTP.KD.ZG": "Real GDP growth %",
    "FP.CPI.TOTL.ZG": "Inflation CPI %",
    "SL.UEM.TOTL.ZS": "Unemployment %",
    "SP.POP.TOTL": "Population",
    "CM.MKT.LCAP.CD": "Listed market cap (US$)",
    "NY.GDP.PCAP.CD": "GDP per capita (US$)",
    "NE.TRD.GNFS.ZS": "Trade % of GDP",
    "BX.KLT.DINV.WD.GD.ZS": "FDI net inflows % GDP",
    "GC.DOD.TOTL.GD.ZS": "Central govt debt % GDP",
    "BN.CAB.XOKA.GD.ZS": "Current account % GDP",
    "FR.INR.RINR": "Real interest rate %",
    "FM.LBL.BMNY.GD.ZS": "Broad money % GDP",
    "PA.NUS.FCRF": "Official FX rate (LCU per US$)",
    "FI.RES.TOTL.CD": "Total reserves incl. gold (US$)",
}

# FRED (St. Louis Fed) — public CSV download, no API key. The deep US macro + market spine.
FRED_SERIES = {
    # rates & curve
    "DFF": "Fed funds effective (1954-)", "DGS3MO": "3M Treasury", "DGS2": "2Y Treasury",
    "DGS10": "10Y Treasury (1962-)", "DGS30": "30Y Treasury", "T10Y2Y": "10Y-2Y spread",
    "T10Y3M": "10Y-3M spread", "MORTGAGE30US": "30Y mortgage (1971-)",
    "BAA10Y": "Baa corp - 10Y spread", "T5YIE": "5Y breakeven inflation",
    # inflation, labor, growth
    "CPIAUCSL": "CPI (1947-)", "CPILFESL": "Core CPI", "PCEPI": "PCE price index",
    "UNRATE": "Unemployment (1948-)", "PAYEMS": "Nonfarm payrolls (1939-)",
    "ICSA": "Initial jobless claims (1967-)", "GDPC1": "Real GDP (1947-)",
    "INDPRO": "Industrial production (1919-)", "RSAFS": "Retail sales", "HOUST": "Housing starts",
    "UMCSENT": "Consumer sentiment (1952-)",
    # money, liquidity, stress
    "M2SL": "M2 money supply (1959-)", "WALCL": "Fed balance sheet", "RRPONTSYD": "Reverse repo",
    "NFCI": "Chicago Fed financial conditions (1971-)", "STLFSI4": "St. Louis financial stress",
    "USREC": "US recession indicator (1854-)",
    # markets
    "VIXCLS": "VIX (1990-)", "NASDAQCOM": "Nasdaq Composite (1971-)", "SP500": "S&P 500",
    "DCOILWTICO": "WTI crude (1986-)", "DCOILBRENTEU": "Brent crude (1987-)",
    "DHHNGSP": "Henry Hub natgas (1997-)", "DTWEXBGS": "Broad US dollar index",
}

# Stooq — long daily index/commodity history, no key. Fail-soft if Stooq changes access.
STOOQ_SYMBOLS = {"^spx": "SPX", "^dji": "DJI", "^ndq": "NASDAQ", "^nkx": "NIKKEI",
                 "^dax": "DAX", "^ukx": "FTSE", "^hsi": "HANGSENG",
                 "xauusd": "GOLD", "xagusd": "SILVER", "cl.f": "WTI_FUT", "hg.f": "COPPER_FUT"}

CELESTRAK_GROUPS = ["active", "stations", "gps-ops", "starlink"]

# Approximate boxes (lat_min, lat_max, lon_min, lon_max). v3.3 CHOKEPOINT WATCH polygons can replace these.
CHOKEPOINTS = {
    "hormuz": (25.8, 27.2, 55.5, 57.5), "malacca": (1.0, 4.0, 99.5, 104.0),
    "suez": (29.8, 31.4, 32.2, 32.7), "bab_el_mandeb": (12.2, 13.2, 42.9, 43.8),
    "panama": (8.8, 9.5, -80.0, -79.4), "bosporus": (40.95, 41.25, 28.95, 29.15),
    "gibraltar": (35.8, 36.2, -5.9, -5.2), "dover": (50.8, 51.2, 1.2, 1.9),
    "taiwan_strait": (22.5, 25.5, 118.5, 121.0),
}

HN_QUERIES = ["finance", "stock market", "tokenization", "federal reserve"]
BLUESKY_QUERIES = ["stock market", "tokenization"]
