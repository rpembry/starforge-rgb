# One registered local producer path

`RegisteredHost` is an opt-in source-only candidate layered on the bound listener. The caller supplies an **existing fresh coordinator** whose host source registry contains exactly the binding's one source and matching capabilities. This keeps the existing coordinator as the only policy arbiter for the path. Its per-source admission budget is therefore the binding's budget for this one-source slice; multi-source binding quotas are not implemented.

The caller explicitly supplies an existing runtime directory. The path check rejects a symlink component, a non-private root, and an ancestor writable by untrusted users; a root-owned sticky `/tmp` ancestor is allowed for synthetic tests. It opens only `<binding_id>.sock` below that directory. The bound listener still checks Linux peer UID and creates a mode-0600 socket. There is no CLI, service registration, path discovery, or live producer hookup.

Each `RegisteredHost` creates a fresh random process session epoch. A same-UID caller first sends `{"proto":1,"op":"hello"}` and receives that epoch, then includes `session_epoch` beside `proto`, `op` and `event` in every `publish` frame. Frames from a previous session fail before coordinator admission after a restart. Within a session, the coordinator still deduplicates event and retry IDs. The epoch is public to authorized same-UID callers; it is **not** a credential and cannot distinguish same-UID processes. Retraction remains unavailable, so old subject names cannot be used to cancel a new generation through this path.

Reproducible synthetic checks:

```sh
PYTHONPATH=src python -m unittest tests/test_registered_host.py -v
PYTHONPATH=src python -m unittest discover -s tests -q
```

The first command exercises a temporary private socket, a fresh session, an old-session replay, one-source rate limit, unregistered coordinator rejection and unsafe path rejection with fake sinks. It skips socket tests if the sandbox denies binding. It makes no device or audio output.

Before any live producer integration, the host needs a protected runtime root on the actual target, an approved mapping from the intended producer to a distinct OS identity or another authentication mechanism, and independent checks of restart, ownership and user-visible behavior on that host. A same-UID process can obtain the current epoch and impersonate the bound source. Generation-safe retraction, private generation receipts and quota sharing across multiple bindings remain separate work.
