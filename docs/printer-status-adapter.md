# Offline P1S completion boundary

`BambuCompletionAdapter` consumes a **host-normalized** observation from a future authorized read-only collector. This slice does not connect to a printer, Bambu account, slicer, MQTT broker, CUPS, or notification output. It has no network or printer-control code, credentials, host address, serial number, or job title. The installed Bambu Studio application is not treated as a documented status feed merely because it exists.

Each normalized observation carries a host-owned connection epoch, an increasing sequence within that epoch, a UTC observation timestamp, a state (`printing`, `paused`, `finished`, `failed`, `idle`, `unknown`), and an opaque per-run job ID when active or terminal. The collector must assign a distinct job ID for each print and increment the epoch after a reconnect. The adapter does not parse raw vendor messages or assume that any individual report is a complete snapshot. Its default freshness bound is 20 seconds, leaving room under the 30-second cue TTL.

Only a fresh, explicit `finished` or `failed` state for the same job **after this process observed it printing in the same connection epoch** emits a semantic event. Paused preserves an already observed run. Idle, unknown, stale reports, out-of-order reports, startup terminal snapshots, and reconnects produce no completion or failure. A source/job terminal emits at most once in the bounded in-memory dedupe window. The event omits printer serial, job name and contents; it uses an opaque digest identifier, fixed generic text, and `printer.completed` or `printer.failed` cue IDs for later theme mapping. The portable coordinator still applies its own dedupe, TTL, quiet and output policy.

This is deliberately conservative: completion during a disconnection can be missed. It must remain unknown rather than becoming a false success or failure. A future collector needs separate review of the exact P1S status interface, authorization, payload normalization, per-run identity, reconnect semantics, and observed transition behavior. An actual source-to-text/audio/RGB path needs a separate local mapping and commissioning gate; this slice does not enable unattended output.

Fake verification:

```sh
PYTHONPATH=src python -m unittest tests.test_printer_status -v
```
