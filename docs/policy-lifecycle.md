# Policy lifecycle contract (issue #5 follow-on)

This slice changes only the portable coordinator and fake tests. Producer event v1 and the local socket format remain unchanged. No service, device, notification or credential is installed or activated.

## Bounded continuous leases

Each event still has a TTL of at most five minutes. A host may set `max_lease_age_s` from 300 to 86400 seconds; the default is 3600 seconds. Repeated events for one `(source_id, subject_id)` retain the first accepted time, and each renewal is clamped to that continuous lease horizon. Whenever a lease ends—at its horizon, by short TTL, or by cancellation—the current baseline or next cue is dispatched and the source/subject pair is retired until `first_seen + 2 × max_lease_age_s`. Thus a new idempotency key cannot immediately resurrect a run after a 1 ms TTL or early cancel. A distinct subject ID is the explicit new-run identity; source adapters must allocate a fresh one per run. Retirement is bounded, not a permanent uniqueness registry: after retention expires, a reused subject ID can be admitted, so adapters must not recycle run IDs. Retired entries and active leases share a bounded 4096-entry budget. Cancellation remains admissible at that capacity. The host controls the limit; producer JSON cannot change it.

Lease, rate, cooldown, deduplication and feedback TTLs use monotonic elapsed time in production. Event freshness and local quiet hours use the wall clock and configured timezone. Tests may inject both clocks separately; if only a fake wall clock is supplied, it also serves as the fake monotonic clock for backward-compatible deterministic tests. Moving the wall clock backward does not extend an already accepted lease when the monotonic clock continues.

## Restart and prior output

`Coordinator.recover_baseline(generation, cue_id, text, ...)` creates a new in-memory coordinator from the host's *current* baseline settings. It loads no old leases, dedupe records or queued sounds. With a baseline cue, it attempts an audio clear before dispatching the baseline without replaying a cue/restore sound; without one, the baseline dispatch clears all channels. `recovery_result.previous_output` stays `unknown`; `audio_clear` and per-channel `baseline_dispatch` report adapter acceptance, failure or unsupported capability separately. Acceptance does not prove that a physical device cleared or a person perceived anything. A host must provide the baseline from protected local settings; the core does not read private configuration.

## Echo suppression and local time

`own_origins` suppresses exact self-origin IDs. A host may call `record_feedback(expected_source_id, correlation_id, ttl_s)` for one expected echo. Only that exact source/correlation pair is suppressed for up to 300 seconds, with at most 1024 registrations; unrelated sources sharing the correlation remain independent. The producer API cannot register a feedback exemption. This is a narrow loop guard, not authenticated source binding.

Quiet windows are evaluated in the configured IANA timezone on each policy tick. The start minute is inclusive and the end exclusive. In a spring-forward gap, nonexistent local minutes are skipped. In a fall-back fold, each repeated minute is evaluated under the same window. Tests cover both transitions and a backward wall-clock jump with advancing elapsed time.

Remaining issue #5 work includes a complete origin/correlation flow across actual desktop and audio adapters, protected baseline configuration, restart behavior against real outputs, broader burst and interruption commissioning, and user-facing history/privacy behavior. Fake acceptance is never physical delivery evidence.
