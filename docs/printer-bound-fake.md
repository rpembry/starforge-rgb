# Printer collector to registered fake outputs

`printer_ingress` adds an opt-in source adapter between the existing subscribe-only P1 report collector and the registered local Unix listener. The collector still emits a semantic `printer.completed` or `printer.failed` event only after its bounded transition proof. `BoundPrinterPublisher` sends that event through `hello` and `publish` to the **existing coordinator**; it does not control the printer or construct audio, RGB or desktop sinks. The synthetic test uses a fake report client and fake text, RGB and audio sinks. No CLI or service starts this path.

The host-owned settings document is a private data file, for example `~/.config/starforge-rgb/printer-ingress.json`:

```json
{"version":1,"allowed_account":"EXISTING_LOCAL_ACCOUNT","runtime_root":"/PRIVATE_RUNTIME_ROOT"}
```

The file must be owned by the running user, mode 0600, in an owned mode-0700 directory. Unknown or duplicate keys, an unknown Linux account, or an account whose resolved UID differs from the host's effective UID fail closed before a listener opens. The account name is resolved through the OS account database. No account, credential, group, ACL or service is created. The setting is never accepted from a producer event. The registered listener then checks kernel peer UID against the host UID. One source is derived from the collector's serial digest; the serial, access code and report bodies are absent from this settings file and from receipts.

The runtime root must already exist, be private, and pass the registered host's ancestor checks. This code does not create it or install a listener. A real target path must be checked outside a rootless test sandbox before use. In the current isolated executor, system ancestors appear as unmapped UID 65534 and the validator deliberately refuses them; that source-test environment does not establish a trustworthy runtime path for a live host.

Reproduce the fake journey:

```sh
PYTHONPATH=src python -m unittest tests/test_printer_ingress.py -v
PYTHONPATH=src python -m unittest discover -s tests -q
```

The first test feeds `RUNNING`, `FINISH`, `FINISH` reports to the collector and checks one completion receipt through the bound listener and coordinator. Its only sinks are fake. Source tests cannot prove a live printer transition, window visibility, sound or RGB hardware behavior. A same-UID process can still impersonate this source, and the session epoch does not prevent rewrapping an old still-fresh event. Live collector network use, the account trust choice, runtime path, actual output sinks and physical commissioning require separate checks.
