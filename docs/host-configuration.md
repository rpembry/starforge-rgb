# Protected host configuration (issue #5 slice)

This is a read-only POSIX host adapter for the current baseline and quiet policy. It does not create a settings file, change permissions, start a service, or connect any output. The portable coordinator and producer event v1 remain platform independent. All examples below are synthetic.

## Version 1 document

```json
{
  "version": 1,
  "baseline": {"generation": 2, "cue_id": "ambient.example", "text": null},
  "quiet": {
    "manual": false,
    "dnd": false,
    "muted": ["audio"],
    "timezone": "UTC",
    "windows": [{"start_minute": 1380, "end_minute": 420}]
  }
}
```

Every field is required; unknown or duplicate keys fail. The document is at most 4096 bytes. `generation` is a positive signed 63-bit integer and must rise on each accepted reload, including a quiet-only change. After missing or invalid settings cause a fail-closed startup, the first valid file may use generation 1; that one-time allowance closes after it loads, so a repeated or lower generation is stale. `cue_id` may be null to clear the baseline. Text may be null or up to 280 printable characters plus line breaks and tabs, and requires a cue. DEL, bidi controls, and other nonprinting characters are rejected. Muted channels are a unique subset of `text`, `rgb`, and `audio`. Up to eight local-time windows use inclusive start and exclusive end minutes from 0 through 1439; equal endpoints mean all day. The timezone must be an installed IANA zone name. No output paths, scripts, theme overrides, credentials, or producer identity controls exist in this schema.

## Private read and failure behavior

The caller supplies a settings path. `read_private_config` opens its immediate parent as an owned private directory and then opens an owned, regular, single-link file with no followed final symlink. The parent must deny group/other access; the file must have mode 0400 or 0600. Reading uses a file descriptor, checks size and metadata before and after the bounded read, and never adjusts host permissions. A writer should create a complete private replacement and atomically rename it into that directory. POSIX support is the initial host boundary; other OS hosts need separate private-file adapters.

`reload_private_config` validates the entire file and a newer generation before changing the coordinator. Missing, insecure, malformed, or stale settings return `invalid_config` without changing the current baseline or quiet policy. The coordinator applies a valid baseline and policy under one lock so a newly restrictive policy does not briefly expose a new baseline. Audio is not replayed when quiet ends. On process recovery, `recover_private_config` uses current valid settings only and discards old leases. If the file is missing or invalid, it starts from a no-baseline, manual-quiet default and dispatches clears through supplied sinks. The recovery result still reports prior physical output as unknown; fake sink acceptance never proves a device cleared or a person saw or heard a cue.

This slice does not include a settings UI or writer, desktop/audio correlation, persistent history, privacy presentation, platform output adapters, or physical commissioning. No settings file or real output is touched by the source tests.
