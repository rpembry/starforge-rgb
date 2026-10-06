# Opt-in bound Unix socket publish slice

`BoundLocalServer` accepts the proposed v1 `publish` envelope on a restricted Unix socket. It reads Linux `SO_PEERCRED`, requires an exact host-supplied UID, then applies one host-owned `IngressBinding` before passing a semantic event to the coordinator. The binding permits exactly one `source_id` and may limit status, severity, confidence, text and subject fields. Duplicate JSON keys, oversized frames and unknown envelope fields fail closed. Receipts contain caller outcomes, never another lease's plan or text.

This is a source-only integration candidate. No CLI command, startup service or real sink creates this listener. The synthetic test demonstrates accepted, duplicate, out-of-scope and wrong-UID frames with fake text/RGB/audio sinks:

```sh
PYTHONPATH=src python -m unittest tests/test_bound_transport.py -v
```

The test binds a temporary mode-0600 socket inside a mode-0700 directory and removes it. It skips socket assertions if the test sandbox denies Unix socket creation. The full source suite is `PYTHONPATH=src python -m unittest discover -s tests -q`.

Peer UID authenticates an operating-system user, **not an individual process**. Another process under the same UID could connect to a listener and assert its bound source, so this slice does not establish safe isolation among same-user producers. It also does not wire Workbench, the printer collector, containers or a remote relay. `retract`, `hello` and the legacy `status: cancelled` event are refused on this listener until host generation ownership and private receipts are integrated. The existing bare-event `LocalServer` remains a manual Phase 0 path and does not acquire peer authentication from this change. Binding-scoped quotas and host configuration remain open work under #53.
