# Phase 0 cue API (v1)

This is a synthetic dry-run host. It writes no RGB, sound or desktop notification. Python 3.11+ and its standard library suffice to run from source:

```sh
PYTHONPATH=src python -m starforge_cues.cli dry-run --file examples/synthetic-cue.json --at 2026-10-03T00:01:00Z
PYTHONPATH=src python -m unittest discover -s tests -v
```

The fixed `--at` time makes the example repeatable. Without it, current UTC time is used and an expired example is suppressed. `--quiet` suppresses all three channels. These commands only plan and call fake adapters.

To exercise local transport, choose a private directory owned by your user (mode 0700). In separate shells:

```sh
mkdir -m 700 /tmp/starforge-cues-demo
PYTHONPATH=src python -m starforge_cues.cli serve --socket /tmp/starforge-cues-demo/cue.sock
PYTHONPATH=src python -m starforge_cues.cli submit --file examples/synthetic-cue.json --socket /tmp/starforge-cues-demo/cue.sock
```

The socket is mode 0600, is never exposed on a network interface and the host refuses an existing socket path. `Ctrl-C` stops it and removes only the socket inode this process created. A partial client frame times out after two seconds; at most 16 client handlers run concurrently. Without `--socket`, the Linux/Unix host uses `$XDG_RUNTIME_DIR/starforge-cues/cue.sock`; portable dry-run works without Unix sockets. The sample's fixed time expires; create a fresh synthetic event for a live submit. No service or startup integration is installed.

## Contract

The transport sends one UTF-8 JSON object followed by newline, up to 8192 bytes. Unknown fields, duplicate JSON keys, invalid types, control characters, excessive text/metadata and unsupported versions are rejected. Required fields:

| Field | Meaning |
| --- | --- |
| `version` | Integer `1` |
| `event_id`, `idempotency_key` | Source-scoped event and retry IDs; 1–80 ASCII identifier characters |
| `cue_id` | Semantic cue name, never a device command |
| `source_id` | Producer assertion; the local socket does not verify a remote identity |
| `confidence` | `known`, `inferred` or `unknown` provenance |
| `origin_id` | Origin for future feedback-loop control |
| `occurred_at`, `observed_at` | UTC RFC3339 timestamps; observation cannot precede occurrence |
| `status` | `started`, `progress`, `succeeded`, `failed`, `needs_attention`, `cancelled`, `unknown` |
| `severity` | `info`, `warning`, `critical` |
| `ttl_ms` | Integer from 1 through 300000, measured from `occurred_at` |

Optional: `subject_id`, `correlation_id` (same identifier bound); `text` (at most 280 characters, absence remains absent); `metadata` (short scalar values under the keys `label`, `phase`, `group`, `count`). Cancellation requires `subject_id`. Producer requests cannot contain devices, RGB values, audio paths, scripts, arbitrary output actions or policy overrides, including through metadata keys. The local socket's directory permissions restrict callers to the user account; it is not a credentialed multi-user or remote identity boundary. Future remote adapters must authenticate and set provenance independently.

## Resolution and outcomes

The coordinator uses a supplied clock, one minute of at most ten accepted events per source, source-scoped event and idempotency deduplication until expiry, and subject-scoped leases. An exact replay is a duplicate; changed content under an existing event or retry ID is a rejected `replay_conflict`. In-memory source, dedupe and lease tables have fixed capacity limits; cancellation of an existing subject bypasses rate and capacity admission, evicting an old replay record if necessary. Source adapters can register supported statuses and whether they offer text or subjects; this host-owned capability list is not accepted from the producer payload. Critical cues outrank warnings, which outrank info. A lower-priority cue can be accepted but its channels are `preempted`. Cancellation and timer-driven expiry dispatch a restored lower-priority cue or a `clear` plan to fake sinks, even without a new event. `unknown` status stays unknown and never becomes `failed` by inference. Full generation-aware hardware baseline restoration, cooldown tuning and user policy are issue #5.

`result=rejected` means validation failed or a replay conflicted. `suppressed` covers stale, future, duplicate, capacity and rate-limited events. `accepted` means policy accepted the event and produced a plan. `tick()` returns `reconciled` when it dispatches a restore or clear plan, otherwise `unchanged`. A replacement cue without text explicitly clears an earlier text output. The coordinator tracks desired and confirmed applied plans per channel; a failed write is retried on that channel for at most three total attempts. A successful channel is never retried, and ambiguous receipts or a transient audio cue are never retried automatically. Each channel independently reports `accepted` (fake adapter accepted dispatch), `failed` (adapter exception), `unknown` (adapter could not confirm), `unsupported`, `absent`, `preempted` or `suppressed` (quiet/mute). No result proves a user saw or heard a signal. Text/RGB/audio sinks negotiate capabilities independently. Real sink adapters, source capability discovery, external theme resolution and platform integrations are later work.

Portable core CI should run the source tests on Linux, macOS and Windows with Python 3.11+. The restricted UDS transport tests run only where AF_UNIX and POSIX permissions exist; Fedora/GNOME adapter and physical seen/heard tests require separate commissioning. Current evidence is source tests only.
