"""Shared plumbing: HTTP with retries, append-only table upserts, raw archive, manifest, health."""
import csv
import datetime as dt
import gzip
import io
import json
import os
import time
import urllib.error
import urllib.request

UA_DEFAULT = "OPHANIM-Databank/1.0 (+https://github.com/MilOs67-collab/ophanim-)"


class FetchError(Exception):
    pass


def _http_get(url, headers=None, timeout=60, retries=3, backoff=2.0, data=None):
    h = {"User-Agent": UA_DEFAULT, "Accept-Encoding": "gzip"}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404, 410, 422):
                break  # retrying will not help
            ra = e.headers.get("Retry-After") if e.headers else None
            wait = float(ra) if ra and ra.isdigit() else backoff * (2 ** attempt)
            time.sleep(min(wait, 60))
        except Exception as e:  # timeouts, resets, DNS
            last = repr(e)
            time.sleep(backoff * (2 ** attempt))
    raise FetchError(f"{url[:160]} -> {last}")


# Tests swap this out for recorded responses.
fetch_impl = _http_get
sleep_impl = time.sleep


def fetch(url, **kw):
    return fetch_impl(url, **kw)


def fetch_json(url, **kw):
    raw = fetch(url, **kw)
    try:
        return json.loads(raw)
    except ValueError:
        raise FetchError(f"{url[:160]} -> not JSON: {raw[:120]!r}")


def fetch_text(url, **kw):
    return fetch(url, **kw).decode("utf-8", "replace")


def pause(seconds):
    sleep_impl(seconds)


# ---------------------------------------------------------------- values
def fmt(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v:  # NaN
            return ""
        if v.is_integer() and abs(v) < 1e17:
            return str(int(v))
        return repr(v)
    return str(v).replace("\r", " ").replace("\n", " ").strip()


def num(s):
    """Parse a numeric string; return None for blanks / '.' / 'N/A'."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return s
    s = str(s).strip().replace(",", "")
    if s in ("", ".", "N/A", "NA", "null", "None", "-"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        f = float(s)
    except ValueError:
        return None
    return None if f != f else f


# ---------------------------------------------------------------- tables
def upsert_csv(path, rows, key, columns=None):
    """Merge rows into a CSV keyed by `key`. Never deletes a row: the bank only compounds.

    New keys are added; existing keys take any non-blank new values (source revisions).
    Returns (added, updated, total).
    """
    key = tuple(key)
    existing = {}
    cols = list(key)
    for c in columns or []:
        if c not in cols:
            cols.append(c)
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            for c in rd.fieldnames or []:
                if c not in cols:
                    cols.append(c)
            for r in rd:
                existing[tuple(r.get(k, "") for k in key)] = r
    added = updated = 0
    for r in rows:
        r = {k: fmt(v) for k, v in r.items()}
        for c in r:
            if c not in cols:
                cols.append(c)
        kk = tuple(r.get(k, "") for k in key)
        if any(x == "" for x in kk):
            continue
        old = existing.get(kk)
        if old is None:
            existing[kk] = r
            added += 1
        else:
            merged = dict(old)
            merged.update({k: v for k, v in r.items() if v != ""})
            if merged != old:
                existing[kk] = merged
                updated += 1
    if added or updated or not os.path.exists(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
            w.writeheader()
            for kk in sorted(existing, key=_sort_key):
                w.writerow(existing[kk])
        os.replace(tmp, path)
    return added, updated, len(existing)


def _sort_key(kk):
    out = []
    for x in kk:
        n = num(x)
        out.append((0, n, "") if isinstance(n, (int, float)) and not _looks_date(x) else (1, 0, x))
    return out


def _looks_date(s):
    return len(s) >= 7 and s[4:5] == "-"


def write_jsonl_gz(path, records, dedupe_key="url"):
    """Daily news files: merge with anything already written today, dedupe, gzip."""
    seen, out = set(), []
    if os.path.exists(path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    seen.add(rec.get(dedupe_key))
                    out.append(rec)
    added = 0
    for rec in records:
        k = rec.get(dedupe_key)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(rec)
        added += 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.GzipFile(path, "wb", mtime=0) as gz, io.TextIOWrapper(gz, encoding="utf-8") as f:
        for rec in out:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return added, len(out)


def read_csv_text(text):
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


# ---------------------------------------------------------------- run context
class Context:
    def __init__(self, bank_dir, raw_dir, mode, today=None, env=None):
        self.bank = bank_dir
        self.raw = raw_dir
        self.mode = mode  # "nightly" | "backfill"
        self.today = today or dt.datetime.now(dt.timezone.utc).date()
        self.env = env if env is not None else os.environ
        self.results = []

    def table(self, *parts):
        return os.path.join(self.bank, "tables", *parts)

    def save_raw(self, name, data):
        """Full untouched payload for the release-asset archive (not committed to the repo)."""
        if not self.raw:
            return
        os.makedirs(self.raw, exist_ok=True)
        if isinstance(data, str):
            data = data.encode("utf-8")
        elif not isinstance(data, (bytes, bytearray)):
            data = json.dumps(data, separators=(",", ":")).encode("utf-8")
        with gzip.GzipFile(os.path.join(self.raw, name + ".gz"), "wb", mtime=0) as f:
            f.write(data)

    def iso(self, days_ago=0):
        return (self.today - dt.timedelta(days=days_ago)).isoformat()


# ---------------------------------------------------------------- manifest + health
DATE_COLS = ("date", "year", "height", "end", "filing_date", "record_date")


def build_manifest(bank_dir, now=None):
    tables = []
    root = os.path.join(bank_dir, "tables")
    for dirpath, _, files in os.walk(root):
        for fn in sorted(files):
            if not fn.endswith(".csv"):
                continue
            p = os.path.join(dirpath, fn)
            with open(p, newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                cols = rd.fieldnames or []
                dcol = next((c for c in DATE_COLS if c in cols), None)
                n, lo, hi = 0, None, None
                for r in rd:
                    n += 1
                    v = r.get(dcol) if dcol else None
                    if v:
                        lo = v if lo is None or _cmp(v, lo) < 0 else lo
                        hi = v if hi is None or _cmp(v, hi) > 0 else hi
            tables.append({
                "path": os.path.relpath(p, bank_dir).replace(os.sep, "/"),
                "rows": n, "columns": cols, "span_col": dcol,
                "first": lo, "last": hi, "bytes": os.path.getsize(p),
            })
    tables.sort(key=lambda t: t["path"])
    news_days = 0
    nroot = os.path.join(bank_dir, "news")
    for _, _, files in os.walk(nroot):
        news_days += sum(1 for f in files if f.endswith(".jsonl.gz"))
    m = {
        "name": "OPHANIM DATA BANK",
        "generated": (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tables": tables,
        "table_count": len(tables),
        "total_rows": sum(t["rows"] for t in tables),
        "total_bytes": sum(t["bytes"] for t in tables),
        "news_days": news_days,
    }
    with open(os.path.join(bank_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)
    return m


def _cmp(a, b):
    na, nb = num(a), num(b)
    if isinstance(na, (int, float)) and isinstance(nb, (int, float)) and not _looks_date(a):
        return (na > nb) - (na < nb)
    return (a > b) - (a < b)


def write_health(bank_dir, ctx, started):
    hdir = os.path.join(bank_dir, "health")
    os.makedirs(hdir, exist_ok=True)
    rep = {
        "run": ctx.mode, "date": ctx.today.isoformat(), "started": started,
        "finished": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ok": sum(1 for r in ctx.results if r["ok"] and not r.get("skipped")),
        "failed": sum(1 for r in ctx.results if not r["ok"]),
        "skipped": sum(1 for r in ctx.results if r.get("skipped")),
        "sources": ctx.results,
    }
    with open(os.path.join(hdir, f"latest-{ctx.mode}.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1)
    rows = [{"date": ctx.today.isoformat(), "run": ctx.mode, "source": r["source"],
             "ok": r["ok"], "added": r["added"], "updated": r["updated"],
             "seconds": r["seconds"], "error": (r["error"] or "")[:200]} for r in ctx.results]
    upsert_csv(os.path.join(hdir, "runs.csv"), rows, key=("date", "run", "source"))
    return rep
