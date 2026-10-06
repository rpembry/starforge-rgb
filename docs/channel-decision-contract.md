# Proposed channel decision and output contract (#51, #54)

This is a design contract, not a runtime change. The first implementation slice should separate channel selection with fake synchronous sinks. Revision-tagged asynchronous output follows only after its receipt and status projection are specified. No real output adapter is activated by this proposal.

## One policy owner

`Coordinator` remains the only owner of admission, lease lifetime, quiet/mute policy, baseline and priority. Under its lock it expires leases, admits the event, and computes immutable channel intents from one lease snapshot. A channel worker receives an intent; it never ranks leases, admits events, reads producer identity, or overrides quiet policy. `LeaseBook` stores lifetimes and offers a winner lookup but does not become another arbiter. Source binding (#53) must be enforced before external producers use this path.

## Selection by channel

| Channel | Decision from the shared lease snapshot |
| --- | --- |
| Text | Ordered live entries for a stack, including lower-priority entries. A bodyless higher-priority cue does not clear a lower entry's text. Quiet/mute clears the view. A consumer may dismiss its local card without changing the source job. |
| RGB | One sustained eligible winner by the current `(severity, sequence)` rank; otherwise the host baseline, otherwise clear. An expired or cancelled winner restores the next eligible lease or current baseline. |
| Audio | One-shot decision on an admitted semantic transition, independent of the RGB winner. A lower RGB-priority cue may sound. A refreshed `needs_attention` state does not replay sound. No cue or restore sound is replayed on unmute, restart or fallback. Quiet/mute drops playback and may request stop. |

Future routed outputs such as Pushover need their own eligibility and once-per-transition rule; they must not be added as another consumer of the RGB winner. `unknown` source status remains unknown and cannot become `failed` by inference. Channel capability checks and failures are independent.

## Intent, revisions and application

Each changed decision receives a process-local monotonically increasing revision. An internal intent contains `(revision, channel, operation, bounded payload, expiry)`. Revisions reset on process restart, when actors and pending state also reset; they are never compared across restarts. The payload uses validated logical cues and text, not driver commands or producer-supplied asset paths. The coordinator publishes desired intents while holding its lock; a worker performs all sink I/O after that lock is released.

Text and RGB each have one pending latest-state slot. A newer revision replaces an unapplied older one; a clear replaces pending state. A write already in flight may finish, so its receipt cannot mark a newer revision applied. The worker then reconciles the latest desired state. Audio has at most one pending one-shot intent; an older pending sound is replaced, never queued. It is dropped if its deadline has passed or it waited more than one second, whichever comes first. An ambiguous playback receipt is never automatically retried. Stop/clear must have a bounded adapter path; a backend unable to stop promptly is not ready for live use. Sink-specific I/O timeouts and retry/backoff stay with the worker. Slow or failed work in one channel cannot hold the coordinator lock or block another channel's worker.

The current synchronous v1 producer receipt keeps its existing meaning during the fake selection slice. Asynchronous application requires a versioned receipt that reports `planned`, `suppressed` or `preempted` for the caller's own event, and a separate bounded revision/status projection for later `applied`, `failed`, `unknown`, `superseded` or `dropped_stale` outcomes (#60, #61, #63). Cancellation and preemption cannot reveal another source's plan or text. A worker's `applied` receipt proves only adapter acceptance, never human perception or physical device state.

## Implementation gates

1. Extract a pure channel selector behind `Coordinator` with a synchronous fake executor. Keep v1 receipts stable. Tests: text stack retains a lower body, RGB fallback, lower-priority audio transition, quiet/mute, decision refresh, and independent capability failure.
2. Add bounded revision-tagged workers and the new receipt/status projection together. Tests: a blocked fake sink does not block admission or other channels; burst coalescing applies only the latest sustained state; old in-flight results cannot overwrite a newer revision; clear and audio age/stop behavior are bounded; unknown remains distinct from failure.
3. Consider real adapters only after source binding (#53), protocol evolution (#63), independent fake QA and per-adapter timeout/stop evidence. No deployment or live device acceptance is implied by these gates.
