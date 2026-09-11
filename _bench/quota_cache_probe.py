"""Repro for github/copilot-sdk#2619: account/getQuota is served from a
process-lifetime cache, and reset_date is the runtime's fetch timestamp.

Calls get_quota twice in ONE runtime process, `--gap` seconds apart, then
once more from a FRESH process. Expected (1.0.8 SDK / CLI 1.0.83, 2026-09-11):
the two same-process snapshots are byte-identical, reset_date included; the
fresh process carries its own, later reset_date. Run a turn on another
surface between the calls to also see used_requests stay frozen.

Usage: python _bench/quota_cache_probe.py [--gap 40]
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from copilot import CopilotClient
from copilot.rpc import AccountGetQuotaRequest

CWD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def fetch(client):
    res = await client.rpc.account.get_quota(AccountGetQuotaRequest())
    snap = res.quota_snapshots["premium_interactions"]
    return snap.used_requests, snap.entitlement_requests, snap.reset_date


async def main(gap):
    client = CopilotClient(working_directory=CWD)
    await client.start()
    try:
        u1, cap, r1 = await fetch(client)
        print(f"{time.strftime('%H:%M:%S')}  same process, call 1: used {u1}/{cap}  reset_date {r1}")
        await asyncio.sleep(gap)
        u2, _, r2 = await fetch(client)
        print(f"{time.strftime('%H:%M:%S')}  same process, call 2: used {u2}/{cap}  reset_date {r2}")
    finally:
        await client.stop()

    fresh = CopilotClient(working_directory=CWD)
    await fresh.start()
    try:
        u3, _, r3 = await fetch(fresh)
        print(f"{time.strftime('%H:%M:%S')}  fresh process:        used {u3}/{cap}  reset_date {r3}")
    finally:
        await fresh.stop()

    print()
    print("same-process reset_date identical:", r1 == r2,
          "(cache hit)" if r1 == r2 else "(re-fetched)")
    print("fresh process reset_date differs: ", r3 != r1,
          "(fetched at startup)" if r3 != r1 else "(unexpected)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=40.0,
                    help="seconds between the two same-process calls")
    asyncio.run(main(ap.parse_args().gap))
