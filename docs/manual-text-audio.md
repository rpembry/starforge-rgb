# Foreground manual text and sound path

This is an opt-in host path for one synthetic notification. It has no service, startup entry, automatic source, RGB writer, system-volume change, credential, or persistent output setting. Importing the modules does not open GTK or PipeWire. The normal CLI `serve` remains fake-output only.

The foreground GTK window can accept events on a restricted Unix socket while rendering the text stack. With `--manual-audio-sink`, it also constructs the existing exact-target PipeWire adapter, mapping only `manual.test` to the original in-memory 500 ms two-note earcon. The normal per-stream gain is 0.05. A one-off 0.15 gain requires both `--manual-audio-gain 0.15` and `--commissioning-override`; this is a software gate, not permission to play it. Closing the window stops its socket and requests owned-audio cleanup. The adapter's process/graph acknowledgment does not prove physical device-buffer drain or that the user heard the cue.

After exact sink, gain, and session approval, an operator can use a protected local configuration with manual quiet off and no selected quiet window, then run the foreground host. An insecure or missing settings file starts in quiet mode; an insecure socket directory prevents the server from starting. These are shape examples, not commands that create or alter private settings:

```sh
PYTHONPATH=src python -m starforge_cues.text_window --serve \
  --socket /PRIVATE_DIR/cue.sock --config /PRIVATE_DIR/settings.json \
  --manual-audio-sink EXACT_SELECTED_SINK_NAME
PYTHONPATH=src python -m starforge_cues.cli manual-submit --socket /PRIVATE_DIR/cue.sock
```

`manual-submit` generates a fresh, fixed-content `manual.local` success event with a 30-second TTL. It does not read Workbench, printer state, browser notifications, or arbitrary message text. Its response reports coordinator/adapter acceptance, never visible or audible perception. Quiet policy can suppress the sound. The host does not replay a muted transient when quiet ends. Actual window visibility, exclusive sink route, heard level, and close/stop behavior need an exact-build in-person commissioning record before calling this path working.

The current local socket permits same-user clients to assert source IDs; it is not a multi-user authentication boundary. Do not expose its directory or treat a same-user assertion as independent provenance. Automatic producers need separate source adapters and review.
