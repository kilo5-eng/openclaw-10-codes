---
name: api-endpoints
description: Master API endpoint index, testing, and management for MBOUM/Fintel/yf/dYdX/Grafana. Docs, curl testers, validation. Use when: (1) checking/correcting endpoints (e.g. 10-73 mboum SI no_data), (2) "api master list/index", (3) testing keys/urls, (4) syncing endpoint docs, (5) 10-code API fixes.
---

# API Endpoints Master Skill

Central hub for all API endpoints. Prioritize paid APIs: MBOUM > Fintel > yf.

## Quick Actions
```
exec scripts/test-endpoint.py --url /v1/markets/quote?ticker=BMNR --type mboum
read references/mboum.md  # full spec
```

## Management Workflow
1. **Test failing endpoint** → `scripts/test-endpoint.py`
2. **Update docs** → append to references/ + write SKILL.md summary
3. **10-code fix** → patch scripts/10-73.py etc. via workspace diffs
4. **Key check** → `echo $MBOUM_API_KEY | wc -c` (nonzero?)

## Key Files
- **references/mboum.md**: Full MBOUM spec/priority
- **scripts/test-endpoint.py**: curl + jq validator
- Update via heartbeat or explicit cron.

Self-review: Expand on new APIs (Polymarket, etc.).