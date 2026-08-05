---
title: nightly-sync-runbook
source: ingested
ingested_at: '2026-08-04T14:47:24.295273+00:00'
---

# Nightly Sync Runbook

Our sn-rag nightly sync runs at 03:00 via a systemd user timer with Persistent=true,
so a run missed while the machine was powered off fires on the next boot instead of
being skipped. The zephyrine reconciliation step must complete before indexing.

## Troubleshooting
If the sync reports INDEX_BUSY, another indexing run holds the lock.
