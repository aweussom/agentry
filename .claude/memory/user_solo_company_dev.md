---
name: user-solo-company-dev
description: "User is the only developer on his Q-Free projects despite the company context; don't frame work in terms of team coordination, SPOF for others, or \"company workload\" risk"
metadata: 
  node_type: memory
  type: user
  originSessionId: 22fb4f79-875d-4c0e-9730-c867c0eaf299
---

User works at Q-Free but is effectively a solo developer across his projects (geomap-united-nations, agentry, and adjacent tooling). Even when a project ships internal services or is wired into production-ish surfaces (e.g. the Sweden NOC dashboard), there is no team to coordinate with — he runs ops, dev, and decisions alone.

**How to apply:**
- Don't surface SPOF, team-onboarding, code-ownership, or "is this safe for the company?" concerns as a default reason to be cautious. These are his calls to make about his own work.
- ToS-gray-zone framing (e.g. agentry's "personal spike, don't expose externally" warning being applied to internal company tools) is similarly his call — note it once if genuinely relevant, then drop it. Repeating it after he's made the call is patronising.
- Apply this to all Q-Free-adjacent projects unless the project itself explicitly involves another developer. There may already be a parallel memory in the geomap project (`feedback_solo_ownership_no_codeowners.md`) — this one captures the same fact from the agentry side so it's available for cross-project substitution discussions like the "use agentry instead of Azure for bench.py" thread.
