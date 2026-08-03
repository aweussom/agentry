---
name: project-agentry-quota-economics
description: "Copilot has TWO separate quotas: monthly premium-request billing (billing API; reads 0 for gpt-5-mini/haiku) vs a rolling-window rate limit for included models (the footer's 'Remaining reqs'; only in copilot's internal endpoint). They don't match."
metadata:
  node_type: memory
  type: project
  originSessionId: 4e0d7769-50ca-4552-a93c-f34f9305f795
---

Copilot exposes (at least) TWO distinct quotas, and agentry can only see one:

1. **Monthly premium-request billing** — billed/metered requests (premium
   models like Claude Sonnet/Opus). This is what the documented GitHub billing
   API (`/users/{u}/settings/billing/premium_request/usage`) returns, and what
   agentry's `copilot.quota_status()` reads.
2. **A short rolling-window rate limit for included/base models** — this is what
   copilot's footer "Remaining reqs: X%" shows. Same primary/secondary-window
   shape codex exposes. It lives only in copilot's internal real-time endpoint,
   NOT in the billing API.

**Empirical (2026-05-31, private account):** the billing API returned
`usageItems: []` (0 used) while the footer simultaneously showed `Remaining
reqs 76%` (~24% used). So gpt-5-mini / haiku-4.5 on a personal account are
almost certainly NOT premium-billed (billing stays 0) — they're throttled by
the rolling window instead. The footer figure is therefore unreachable via the
billing API; agentry's meter reads 0 unless you actually run premium models.

**Work Q-Free account (EMU):** gpt-5-mini unmetered; billing API blocked
(400/403, enterprise-managed) → copilot quota both unavailable and irrelevant.
User runs agentry on gpt-5-mini here, so quota doesn't matter.

`agentry.ini` is left configured with the private account's PAT, but note the
meter shows monthly *premium billing* (0 for gpt-5-mini use), not the footer's
rolling-window number.

**Takeaway:** don't conflate the footer's "Remaining reqs" with premium-request
billing — they're different counters. "What's free" on personal plans stays
murky (promos, model churn). Relates to [[project-agentry-backend-tiers]],
[[project-codex-backend-investigation]], [[project-codex-prompt-caching]].
