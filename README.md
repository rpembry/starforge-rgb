# Starforge RGB

A small, local lighting bridge for a Linux workstation. The goal is calm ambient
lighting, optional music-reactive effects, and short, unobtrusive cues from
Starforge Workbench or other local tools.

This repository is a scaffold, not a running integration. Nothing here currently
changes lighting, fan speed, or Workbench state.

## Intended shape

```text
Workbench attention feed ─┐
Other local tools ─────────┼─> local cue API ─> one lighting arbiter ─> OpenRGB
Music input (opt-in) ──────┘
```

- One process owns lighting writes so effects cannot fight each other.
- Ambient lighting is the fallback; temporary cues expire and restore it.
- Music-reactive mode is opt-in. Alerts may briefly override it, then return.
- The API binds to loopback (or a permission-restricted Unix socket), never a
  public interface. OpenRGB's SDK server must also bind to loopback.
- Workbench integration reads its existing authenticated attention API. An
  observation alone does not authorize action or imply task failure.
- No credentials, private Workbench data, or machine-specific runtime settings
  belong in Git.
- RGB control must not change thermal fan curves or fan speeds.

## First experiment

1. Identify which OpenRGB motherboard zones actually drive the visible fans.
2. Investigate why the RGB memory is not detected before claiming synchronized
   RAM control; do not send speculative SMBus/I2C writes.
3. Implement a dim ambient effect and a manually triggered, time-limited cue.
4. Add an authenticated local cue API with bounded brightness, rate limits, and
   automatic expiry.
5. Add an opt-in Workbench attention client and music mode after the basic
   lighting behavior is verified in person.

The initial Workbench integration should be a local client of Workbench's API,
not a new remote RGB endpoint or a change to Workbench's database.

## License and contact

Original project code is licensed under the [MIT License](LICENSE). Dependencies
and theme or audio assets, if added, retain their own licenses.

Project contact: randall@embry.com.
