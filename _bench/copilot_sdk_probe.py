"""Copilot SDK probe: model availability, AI-credit pricing, latency, caching.

Drives the same github-copilot-sdk transport as agentry's CopilotSDKBackend
(bench.py's `copilot --acp` path is retired). Per model it verifies the
session pin round-trips (get_current), then times N short turns in ONE
session so prompt caching shows up: turn 1 pays the cache write for the
system prompt, later turns should bill cache reads at ~1/10 the fresh-input
rate (verified on gpt-5.6-luna 2026-08-13; TTL 30 min).

Usage (from the repo root, venv python):
    python _bench/copilot_sdk_probe.py [model ...] [--turns N] [--cwd DIR]

No models given -> table only. Credits: 1 AIC = $0.01; billing.tokenPrices
in models.list are credits per 1M tokens.
"""
import argparse
import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from copilot import CopilotClient
from copilot.rpc import ModelsListRequest, PermissionDecisionReject
from copilot.session_events import (
    AssistantMessageDeltaData, AssistantUsageData,
    SessionErrorData, SessionIdleData)


def print_model_table(models):
    print(f"{'id':<22} {'price':<7} {'in':>6} {'out':>6} {'c-read':>6} "
          f"{'c-write':>7} {'ctx':>9} efforts")
    for m in models:
        tp = (m.get("billing") or {}).get("tokenPrices") or {}
        lim = (m.get("capabilities") or {}).get("limits") or {}
        efforts = m.get("supportedReasoningEfforts") or []
        print(f"{m.get('id', '?'):<22} "
              f"{m.get('modelPickerPriceCategory') or '-':<7} "
              f"{tp.get('inputPrice', 0):>6.0f} {tp.get('outputPrice', 0):>6.0f} "
              f"{tp.get('cacheReadPrice', 0):>6.1f} {tp.get('cacheWritePrice', 0):>7.1f} "
              f"{lim.get('max_context_window_tokens', 0):>9,} "
              f"{','.join(efforts) or '-'}")


async def probe_model(client, cwd, model, n_turns, effort):
    loop = asyncio.get_event_loop()
    session = await client.create_session(
        working_directory=cwd, streaming=True, available_tools=[],
        on_permission_request=lambda req, inv: PermissionDecisionReject(
            feedback="bench probe, no tools"),
        model=model, reasoning_effort=effort)
    cur = await session.rpc.model.get_current()
    pin_ok = cur.model_id == model
    print(f"\n=== {model} @ {effort}  (runtime says {cur.model_id!r}"
          f"{'' if pin_ok else '  *** PIN OVERRIDDEN ***'}) ===")
    rows = []
    for i in range(1, n_turns + 1):
        done = loop.create_future()
        st = {"t0": 0.0, "ttfb": None, "text": [], "credits": 0.0,
              "cache_read": 0, "cache_write": 0}

        def on_event(ev, st=st, done=done):
            d = ev.data
            if isinstance(d, AssistantMessageDeltaData) and d.delta_content:
                if st["ttfb"] is None:
                    st["ttfb"] = time.monotonic() - st["t0"]
                st["text"].append(d.delta_content)
            elif isinstance(d, AssistantUsageData):
                cu = d.copilot_usage
                nano = (cu.get("totalNanoAiu") if isinstance(cu, dict)
                        else getattr(cu, "total_nano_aiu", 0)) if cu else 0
                st["credits"] += (nano or 0) / 1e9
                st["cache_read"] += d.cache_read_tokens or 0
                st["cache_write"] += d.cache_write_tokens or 0
            elif isinstance(d, (SessionIdleData, SessionErrorData)):
                if not done.done():
                    done.set_result(isinstance(d, SessionIdleData))

        unsub = session.on(on_event)
        st["t0"] = time.monotonic()
        await session.send("Reply with exactly: OK")
        ok = await asyncio.wait_for(done, timeout=180)
        total = time.monotonic() - st["t0"]
        try:
            unsub()
        except TypeError:
            pass
        reply = "".join(st["text"]).strip().replace("\n", " ")[:30]
        print(f"  turn {i}: ttfb={st['ttfb'] or -1:.2f}s total={total:.2f}s "
              f"credits={st['credits']:.4f} cacheR={st['cache_read']} "
              f"cacheW={st['cache_write']} ok={ok} -> {reply!r}")
        rows.append((st["ttfb"], total, st["credits"]))
    await session.disconnect()
    ttfbs = [r[0] for r in rows if r[0] is not None]
    if ttfbs:
        print(f"  SUMMARY {model}: ttfb median={statistics.median(ttfbs):.2f}s "
              f"total median={statistics.median(r[1] for r in rows):.2f}s "
              f"credits total={sum(r[2] for r in rows):.4f}")
    # Machine-parseable line, same convention as the other probes.
    print(f"BENCH_RESULT model={model} effort={effort} n={len(rows)} "
          f"ttfbs={','.join(f'{r[0]:.3f}' for r in rows if r[0] is not None)} "
          f"totals={','.join(f'{r[1]:.3f}' for r in rows)} "
          f"credits={','.join(f'{r[2]:.5f}' for r in rows)}")


async def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("models", nargs="*",
                    help="model ids to probe (none: print the table only)")
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--effort", default="low")
    ap.add_argument("--cwd", default=os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))),
        help="session cwd; decides which copilot-instructions.md loads")
    args = ap.parse_args()

    client = CopilotClient(working_directory=args.cwd)
    await client.start()
    st = await client.get_auth_status()
    print(f"auth: login={st.login} host={st.host}")

    ml = await client.rpc.models.list(ModelsListRequest())
    models = [m.to_dict() for m in ml.models]
    print(f"\n{len(models)} models available (credits per 1M tokens; 1 AIC = $0.01):")
    print_model_table(models)

    known = {m.get("id") for m in models}
    for model in args.models:
        if model not in known:
            print(f"\n=== {model}: *** NOT AVAILABLE on this account ***")
            continue
        await probe_model(client, args.cwd, model, args.turns, args.effort)

    await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
