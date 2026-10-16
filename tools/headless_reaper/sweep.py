#!/usr/bin/env python
"""Standalone orphan sweeper for the orchestrator (Tracker #44).

Default = DRY RUN: reports how many leaked verification browsers there are and
how much memory they hold, touches nothing.  --reap kills them (by marker only,
never by image name) and removes their profile dirs.

    python sweep.py                 # report only
    python sweep.py --reap          # reap leftovers older than --max-age-min
    python sweep.py --reap --max-age-min 0   # reap ALL marked leftovers now
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from headless_reaper import reaper


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reap", action="store_true",
                    help="kill leftovers + remove dirs (default: dry run)")
    ap.add_argument("--max-age-min", type=float,
                    default=reaper.STALE_GRACE_SEC / 60.0,
                    help="only touch marker profiles older than this (min)")
    args = ap.parse_args()

    n_all, mem_all = reaper.browser_family_report()
    grace = max(args.max_age_min * 60.0, 0.0)
    res = reaper.sweep_stale(grace_sec=grace, reap=args.reap)

    stale = res["stale_procs"]
    stale_mem = sum(s["rss"] for s in stale)
    mode = "REAPED" if args.reap else "DRY RUN — nothing killed"
    print(f"[sweep] browser-family processes on box (ALL, incl. owner's): "
          f"{n_all}  ({mem_all / 2**20:.0f} MB) — counted only, never killed by name")
    print(f"[sweep] {mode}: {len(stale)} orphan verification process(es), "
          f"{stale_mem / 2**20:.0f} MB (marker prefix '{reaper.PREFIX}-', "
          f"older than {args.max_age_min:g} min)")
    for s in stale:
        print(f"    pid {s['pid']:>7}  {s['rss'] / 2**20:6.0f} MB  {s['dir']}")
    if args.reap:
        print(f"[sweep] killed pids: {[k['pid'] for k in res['killed']]}")
        print(f"[sweep] profile dirs removed: {len(res['removed_dirs'])}")
        for d in res["locked_dirs"]:
            print(f"[sweep] dir still locked (process already dead, next sweep "
                  f"will retry): {d}")
    print(f"[sweep] fresh runs left alone: {res['fresh_skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
