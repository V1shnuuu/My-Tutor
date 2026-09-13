# Monitoring (all free tiers)

## Uptime — UptimeRobot (50 monitors, 5-min interval)
- `GET https://api.<domain>/healthz` → alert if not 200 for 2 checks. Also ping the HF Space standby URL: it keeps the Space awake.

## Metrics — Grafana Cloud Free (10k series, 14-day retention)
Add a Prometheus scrape (Grafana Agent / Alloy on the VM, or Grafana's "Metrics endpoint" integration) for
`https://api.<domain>/metrics`, interval 60 s. Series exported by the core:

| metric | meaning |
|---|---|
| `tutor_provider_budget_used_ratio{provider,window}` | fraction of a provider's daily request/token window used (0–1) |
| `tutor_provider_errors_total{provider}` | upstream errors per provider (process lifetime) |
| `tutor_stt_budget_used_ratio` | Groq Whisper daily window used |
| `tutor_queue_depth` | requests currently waiting for an LLM slot |
| `tutor_events_24h{kind}` | chat / cache_hit / gate_refuse / floor / queue / stt counts, trailing 24 h |
| `tutor_corpus_chunks`, `tutor_uptime_seconds` | sanity |

## Alert rules (Grafana → Telegram/Discord contact point)
```
# a provider is burning its day too fast (fires before 14:00 UTC if > 70 % used)
max by (provider) (tutor_provider_budget_used_ratio) > 0.7 and hour() < 14
# queue is building
tutor_queue_depth > 5                                   for 5m
# cache is not doing its job (after warm-up week)
tutor_events_24h{kind="cache_hit"} / (tutor_events_24h{kind="chat"} + tutor_events_24h{kind="cache_hit"}) < 0.25
# floor answers are becoming common → providers exhausted or down
tutor_events_24h{kind="floor"} > 100
# STT budget
tutor_stt_budget_used_ratio > 0.8
# host
node_load1 > 3   (node_exporter on the VM)   ·   disk > 80 %
```

## Frontend errors — Sentry Free (5k events/month)
`npm i @sentry/react`, init in `web/src/main.tsx` with `VITE_SENTRY_DSN`; sample rate 0.2.

## Weekly checklist (5 minutes)
1. Grafana: any provider > 50 % of its window at peak? Re-verify that provider's free-tier limits in `core/providers.yaml`.
2. `GET /admin/status`: cache entries growing, `avg_ms_24h.chat` < 1500.
3. Litestream: last snapshot in the R2 bucket is < 1 h old.
