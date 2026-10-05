# P1S foreground collector candidate

This is a software-only, user-run candidate. The collector has no printer command or MQTT `PUBLISH` method. It opens a TLS connection to a private IPv4 address, authenticates only after CA validation and an exact peer-certificate SHA-256 match, subscribes to `device/<serial>/report`, and accepts bounded JSON reports. The only live command prints the generic semantic cue ID to the terminal; it does not dispatch a desktop, sound, or RGB notification, install a service, or start automatically. Neither the developer tests nor this document connect to a printer.

## Evidence and limits

The maintained [ha-bambulab integration](https://docs.page/greghesp/ha-bambulab) documents P1 read access on current firmware and a [print finished trigger](https://docs.page/greghesp/ha-bambulab/device-triggers). Its [setup guide](https://docs.page/greghesp/ha-bambulab/setup) documents local IP, serial, and access code. Static review of its public source shows a `device/<serial>/report` MQTT subscription and `print.gcode_state` values `RUNNING`, `PAUSE`, `FINISH`, `IDLE`, `FAILED`. It also sends `pushall` requests for full snapshots; this candidate deliberately does not, so a sparse or missing status stream can miss a transition. Its trigger guide says `Print failed` is X1-only, so this P1S collector treats `FAILED` as unknown.

Reports can be partial. This candidate requires an explicit nonempty `print.task_id` **on every `RUNNING`, `PAUSE`, and `FINISH` state report**. Missing or changed IDs suppress completion. The task ID is hashed in memory before the existing completion adapter sees it, and event text is fixed. A report gap over 20 seconds, reconnect, unknown state, malformed report, or terminal snapshot invalidates transition proof. If the P1S does not provide a stable task ID with these reports, no completion will be emitted; a real authorized observation must settle that before relaxing the contract. A live completion and visible or audible output remain unverified.

## Operator-controlled setup for a later authorized run

On the P1S screen, open **Settings → WLAN** to view the LAN IP and access code; scroll to the bottom of **Settings** for the serial. The [P1S setup walkthrough](https://help.simplyprint.io/en/article/connecting-bambu-lab-printers-to-simplyprint-xqlhtd/) shows these screens. Viewing them does not require switching to LAN-only mode. [ha-bambulab](https://docs.page/greghesp/ha-bambulab) states read access continues on the cited current firmware; LAN-only and Developer Mode are relevant to restoring third-party write control and can affect Bambu Cloud access. Do not change those modes for this status-only candidate.

The access code belongs in an owned `0700` directory and an owned `0600` file. The default path is `~/.config/starforge-rgb/credentials/bambu-p1s-access-code`. The collector refuses group/world-readable files, symlinks, nonregular files, and unexpected length or format. It never takes the code as a command-line argument, prints it, or stores it in Git. This is a broad printer access credential despite the client's subscribe-only behavior. Review any decision to grant ongoing network use of it separately.

The Bambu LAN TLS certificate may not match an IP address. This candidate requires both a trusted CA PEM and a confirmed exact peer leaf fingerprint; it never disables chain validation or silently accepts a new peer. A possible CA source is the public [`bambu.cert` from ha-bambulab](https://github.com/greghesp/ha-bambulab/blob/0e027ff135a6d9265cb756d3e246747954c76722/custom_components/bambu_lab/pybambu/certs/bambu.cert) at the pinned revision. Its file SHA-256 is `36f2bcee347ec7adce719b5fd350099591a4d3d0ec4e039c7019890d78e152a0`; its X.509 certificate fingerprint is `E9:8F:19:57:8B:3F:12:4A:CE:6B:8A:24:7F:FE:DA:52:DC:99:C8:9F:D4:E7:D2:0C:82:82:99:77:B7:F3:35:02`. The operator should obtain and verify that public certificate independently before a run. For example, after approving a public certificate download, the operator can run:

```sh
mkdir -p -m 700 "$HOME/.config/starforge-rgb/trust"
curl -fsSLo "$HOME/.config/starforge-rgb/trust/bambu-ca.pem" 'https://raw.githubusercontent.com/greghesp/ha-bambulab/0e027ff135a6d9265cb756d3e246747954c76722/custom_components/bambu_lab/pybambu/certs/bambu.cert'
sha256sum "$HOME/.config/starforge-rgb/trust/bambu-ca.pem"
```

The reported checksum must match the file SHA-256 above before the certificate is trusted. Certificate rotation or a different printer chain requires explicit review, never an insecure fallback.

After the operator approves one read-only connection, run from the repository root with `PYTHONPATH=src python -m starforge_cues.cli printer-cert-probe --host <private-IP> --ca-file <verified-CA-PEM>`. This displays the CA-validated leaf fingerprint **without reading the access code**. The operator must confirm that the IP is the intended printer and explicitly accept that fingerprint; this initial association is trust on first use, not a manufacturer-attested serial binding. For a later foreground observation, run `PYTHONPATH=src python -m starforge_cues.cli printer-observe --host <private-IP> --serial <serial> --ca-file <verified-CA-PEM> --peer-sha256 <confirmed-leaf-fingerprint>`. This reads the default private credential file only after the TLS peer passes both checks. An explicit `--credential-file` path can select another private file. The command produces only generic `semantic cue: printer.completed` output and ends on Ctrl-C or connection failure. It does not reconnect or start in the background.

Any actual text/audio/RGB notification needs a separately reviewed mapping from the semantic callback into the existing local coordinator and a deliberate live commissioning test. Nothing in this collector proves the current printer state or notification delivery.

Fake-only verification:

```sh
PYTHONPATH=src python -m unittest tests.test_printer_mqtt -v
```
