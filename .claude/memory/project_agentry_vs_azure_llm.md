---
name: project-agentry-vs-azure-llm
description: Empirical agentry+Copilot vs Azure OpenAI comparison run 2026-05-26 on the Sweden LLM matcher/summarizer — agentry is ~2× slower per call but cheaper and faster at batch scale
metadata: 
  node_type: memory
  type: project
  originSessionId: 22fb4f79-875d-4c0e-9730-c867c0eaf299
---

Single-call latency comparison ran 2026-05-26 against the Sweden
geomap LLM matcher (`q-free/geomap-united-nations/sweden/llm-benchmark/
summarize-tickets.py`). Same ticket (IN-100), same model family
(gpt-5-mini), matched reasoning effort:

| Backend | Reasoning | Latency |
|---|---|---|
| Azure chat.completions | default (medium) | **6.6s** |
| agentry → Copilot CLI ACP | medium | **14.2s** |

**Per-call:** agentry ≈ 2.1× slower; proxy tax ≈ 7.6s/call.

**Batch scale flips the comparison.** Azure's 10K-TPM cap fires ~46s
throttle sleeps every ~3-4 calls at ~2.4K tokens each. Back-of-envelope
on a 100-ticket cold summarize pass:
- Azure: ~24 calls × 7.5s + ~24 throttle sleeps × 46s ≈ **32 min**
- agentry: 100 × 14.2s, no throttle ≈ **24 min**

agentry wins wall-clock on batch despite being slower per-call.

**Quality:** equivalent on this sample. Both backends produced
indistinguishable summaries ("Multiple nodes (PSU, LCC, IOC) across
stations 011, 012 ..."). Not comprehensively benchmarked — n=1.

**Cost:** agentry = $0 marginal (Q-Free pays the Copilot seat, gpt-5-mini
is on the 0× quota tier). Azure ≈ $0.02/100 tickets. Settled.

**Asymmetric upgrade on agentry:** can run gpt-5-mini at
`reasoning=high` at the same 0× cost. Azure's gpt-5-mini default can't
go higher without changing tiers or models. If quality-via-reasoning
ever matters, agentry has headroom Azure doesn't.

**Why:** User wanted to test whether agentry could substitute for
Azure on the Sweden matcher, both for cost and for cheap access to
higher reasoning. User is the only Q-Free dev on this — see
[[user-solo-company-dev]] — so ToS/SPOF cautions on agentry's
"personal spike" framing don't apply.

**How to apply:**
- For `bench.py` (5-min matcher cycle): either works; difference is
  well under cycle time. Pick on cost → agentry.
- For `summarize-tickets.py` batch passes: agentry is faster wall-clock
  due to no TPM throttle. Use agentry.
- For ad-hoc single-call paths where latency matters per-call: Azure.
- Quality wasn't deeply benchmarked. If a real quality gap surfaces
  via `diff runs/summarize-<ts>/<key>.txt`, that overrides the
  latency-cost tradeoff above.

**Re-test triggers:**
- Azure raises the 10K TPM cap → recompute batch crossover.
- Copilot bumps gpt-5-mini above 0× quota → cost story changes.
- agentry adds parallelism (currently single-process, strictly
  sequential — turn_lock in agentry.py) → batch advantage grows.
- Azure's /responses API unlocks for this resource (re-test scheduled
  2026-08-06 per the geomap-side `project_azure_openai_quirks.md`).

**Reproduction recipe** (so future-me can re-run this in one minute):

```powershell
# Prereq: agentry on :8765, AZURE_API_KEY set, WHATSUP_DIR set, fetch-tickets done.
cd C:\devel\q-free\geomap-united-nations\sweden\llm-benchmark
python summarize-tickets.py --backend agentry --reasoning-effort medium --force --key IN-100
python summarize-tickets.py --backend azure   --force --key IN-100
```
