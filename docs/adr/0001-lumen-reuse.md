# ADR 0001: Own the semantic coordinator

Status: accepted for the Phase 0 implementation slice (issues #2 and #3).

## Decision

Use a small Python standard-library core as the sole event/policy arbiter. Keep device and platform I/O behind channel adapters. Do not fork, install or execute Lumen for this slice. A later RGB adapter may use its OpenRGB ideas or a separately reviewed library; it must receive a resolved plan and never arbitrate independently. Any copied Lumen code must preserve its MIT notice. This slice copies none.

## Evidence at pinned Lumen v0.8.5 (`315b199`)

- [Architecture](https://github.com/Brxerq/lumen/blob/v0.8.5/docs/ARCHITECTURE.md): Python process with EventBus, rules, EffectPlayer, device plugins, integrations and local HTTP dashboard. The device contract offers zones and OpenRGB is a built-in adapter.
- [Events](https://github.com/Brxerq/lumen/blob/v0.8.5/src/lumen/core/events.py): `Event` has type, source, free-form data and timestamp. It has no version, subject, provenance, lease or idempotency field.
- [Effects](https://github.com/Brxerq/lumen/blob/v0.8.5/src/lumen/core/effects.py): `_active[device.id]` stores one transient, so a later transient replaces an earlier one. Its quiet check excludes `notify` and `sound`; sound dispatch starts workers. Base color restoration is per device, not a cross-channel lease policy.
- [Sound](https://github.com/Brxerq/lumen/blob/v0.8.5/src/lumen/devices/sound.py): built-in tones become WAV files and playback uses `winsound`, `afplay`, or spawned Linux players. There is no theme asset/output-device contract or reliable perception receipt.
- [Plugins](https://github.com/Brxerq/lumen/blob/v0.8.5/docs/PLUGINS.md) and [API](https://github.com/Brxerq/lumen/blob/v0.8.5/docs/API.md): plugin entry points execute Python; API accepts free-form dotted events and device-specific rule actions over loopback HTTP. Plugins cannot serve as untrusted data-only themes.

## Fit check

| Requirement | Lumen v0.8.5 | Phase 0 choice |
| --- | --- | --- |
| Priority/preemption, multiple TTL leases, cancellation, dedupe | One transient per device; no semantic lease or idempotency contract | One portable policy owner, deterministic clock |
| Quiet policy across text/RGB/audio | RGB transients suppressed, notify and sound exempt | Channel-wide policy before independent dispatch |
| Independent output failure | Device failures isolated in player; no per-channel semantic result | Explicit accepted, skipped, failed, unknown outcomes |
| Data-only external themes | Executable Python plugins and device actions | Versioned safe theme contract in later issue #6 |
| OpenRGB zones | Existing device capability, useful later | Adapter opportunity, no code reuse yet |
| Desktop/remote/container producers | Python integration hooks and local HTTP event API | Producers normalize into one versioned contract |

Extending Lumen cleanly would require replacing its event schema, rule/action boundary, player arbitration and quiet behavior. Wrapping its engine would create two competing arbiters. A large fork would require carrying desktop, dashboard, sound and plugin behavior unrelated to the target core. A small core costs a modest amount of new policy code but keeps one owner and a portable boundary.

The runnable synthetic scenario is `python -m starforge_cues.cli dry-run --file examples/synthetic-cue.json` after adding `src` to `PYTHONPATH`. Tests use fake sinks and clock; no Lumen code or hardware is run. Real visible RGB, audible audio, desktop integration, Windows/macOS adapters and perception remain untested. Original code is MIT; any future third-party code and every media asset retain distinct licensing review.
