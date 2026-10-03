# Policy and local theme foundation

This draft slice extends the fake-output core only. It does not activate a theme, import an archive, decode or play audio, write RGB, show desktop text, send Pushover, or change Workbench state.

## Deterministic policy

`Coordinator.set_baseline(generation, cue_id, text)` accepts a monotonically increasing host-owned generation. A temporary cue keeps precedence until cancellation or expiry, then fake sinks receive the *current* baseline generation. `cue_id=None` clears it. Restart creates a new coordinator with no transient leases; a host must load its current local baseline explicitly.

`QuietPolicy` supports manual global quiet, DND, per-channel mute and local-minute quiet windows. Windows are start-inclusive/end-exclusive; a start after the end spans midnight, and equal endpoints mean all day. The host supplies a timezone and clock; local-time changes are evaluated afresh on each tick. A newly muted channel is cleared. On unmute, text and RGB restore the current desired plan; old transient audio is not replayed. Expiry clear remains allowed during quiet policy. Optional source cooldown and exact self-origin filtering are host-owned. Producer events cannot override policy. This slice does not yet include full lease renewal horizons, persistent bounded history, dynamic lock-screen privacy, or all #5 acceptance tests.

## Local theme preview

The version 1 manifest contains ID/version, author/license/provenance, declared channel capabilities, semantic cue mappings and licensed asset references. Text uses a bounded prefix that is HTML escaped together with actual event text; missing text remains silent. RGB uses logical zone IDs, bounded colors and a level limited by the host cap. Audio references a relative WAV path and SHA-256 digest; no audio bytes are decoded or played. Missing cues/channels resolve to explicit silence.

`load_directory` accepts only `manifest.json` and declared flat `assets/*.wav` files, with fixed manifest/file/count/total-size bounds. It rejects unknown manifest keys, duplicate JSON keys, unsafe paths (including Windows drive/UNC/reserved names), casefold collisions, symlinks, hardlinks, unlisted files, missing hashes/licenses/provenance and unexpected directories. Local directory validation is a preview; archive import, decoded media checks, atomic selection/rollback and last-known-good activation remain #6 work. Asset licenses and provenance are supplied by pack authors and require real rights review before redistribution.

Reproducible synthetic preview:

```sh
mkdir -m 700 /tmp/starforge-theme-demo
cp examples/minimal-theme.json /tmp/starforge-theme-demo/manifest.json
PYTHONPATH=src python -m starforge_cues.cli theme-preview --directory /tmp/starforge-theme-demo --file examples/synthetic-cue.json --rgb-cap 0.1
PYTHONPATH=src python -m unittest discover -s tests -v
```

The CLI returns logical plans only. The project still has no physical output adapter, theme activation control, remote listener, or live credentials.
