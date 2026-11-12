"""OPHANIM DATA BANK runner.

  python -m tools.databank.run --mode nightly            # every night (GitHub Actions)
  python -m tools.databank.run --mode backfill           # one time: deepest history available
  python -m tools.databank.run --mode backfill --only fred,fx_ecb
"""
import argparse
import datetime as dt
import sys
import time

from . import common as C
from . import sources as S


def run(mode, bank, raw=None, only=None, today=None, env=None):
    ctx = C.Context(bank, raw, mode, today=today, env=env)
    started = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for name, modes, fn in S.REGISTRY:
        if mode not in modes or (only and name not in only):
            continue
        t = S.Tally()
        t0 = time.monotonic()
        err = None
        try:
            fn(ctx, t)
        except Exception as e:  # isolate every source
            err = f"{type(e).__name__}: {e}"
        res = {"source": name, "ok": err is None, "added": t.added, "updated": t.updated,
               "seconds": round(time.monotonic() - t0, 1), "skipped": t.skipped and err is None,
               "error": err, "notes": t.notes}
        ctx.results.append(res)
        flag = "FAIL" if err else ("SKIP" if t.skipped else "OK  ")
        print(f"[{flag}] {name:18s} +{t.added:<7d} ~{t.updated:<6d} {res['seconds']:>6}s"
              f"{'  ' + err if err else ''}{'  (' + '; '.join(t.notes[:3]) + ')' if t.notes else ''}",
              flush=True)
    rep = C.write_health(bank, ctx, started)
    m = C.build_manifest(bank)
    print(f"\nDATA BANK: {m['table_count']} tables · {m['total_rows']:,} rows · "
          f"{m['total_bytes'] / 1e6:.1f} MB · {m['news_days']} news days · "
          f"{rep['ok']} ok / {rep['failed']} failed", flush=True)
    return rep


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["nightly", "backfill"], required=True)
    p.add_argument("--bank", default="databank")
    p.add_argument("--raw", default=None, help="directory for full raw payloads (archived as release assets)")
    p.add_argument("--only", default="", help="comma-separated source names")
    a = p.parse_args(argv)
    only = {s.strip() for s in a.only.split(",") if s.strip()} or None
    rep = run(a.mode, a.bank, a.raw, only)
    # Fail the job only if EVERYTHING failed — partial data is still data for the bank.
    return 1 if rep["ok"] == 0 and rep["failed"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
