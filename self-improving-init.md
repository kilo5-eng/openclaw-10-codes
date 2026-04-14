# Self-Improving Init (2026-04-12)

## Patterns Learned
1. **API Stability:** Paid APIs (MBOUM/Fintel) first, fallback to free JSON (Yahoo/metals.live), then HTML grep. jq required for robustness.
2. **Cron Discipline:** Telegram sync was too chatty (3s). Removed. Only 10-11-daily (9 AM) stays.
3. **Model Cost:** Haiku 4.5 ($0.80/$4.00 per M tokens) vs Grok free. Precision matters; speed trade-off acceptable.

## Corrections Log
- ❌ Telegram 3s cron spam → ✅ Removed (11:09 EDT)
- ❌ Grep-based parsing → ✅ JSON + jq (v4 stable)
- ❌ Monolithic cron → ✅ Modular heartbeat tasks

## Next Actions
1. Test 10-73-slv-v4.sh with MBOUM_API env set
2. Link ontology queries to MEMORY.md (cross-skill state)
3. Wire github auto-sync for eth_sfr on heartbeat

## Skills Integrated
- self-improving: Learning/corrections (this file)
- ontology: 10-73 entities + relations
- github: eth_sfr PR monitor
- session-logs: Historical search
- clawhub: Skill versioning
