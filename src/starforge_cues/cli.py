"""No physical outputs: dry-run and fake-output UDS host only."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from .contract import ContractError, CueEvent, MAX_BYTES
from .core import CHANNELS, Coordinator, FakeSink, QuietPolicy


def _raw(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read(MAX_BYTES + 1)
    with open(path, "rb") as stream:
        return stream.read(MAX_BYTES + 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic semantic cue host (no real outputs)")
    commands = parser.add_subparsers(dest="command", required=True)
    dry = commands.add_parser("dry-run", help="resolve one event locally with fake sinks")
    dry.add_argument("--file", required=True, help="JSON event file or - for stdin")
    dry.add_argument("--quiet", action="store_true")
    dry.add_argument("--at", help="UTC RFC3339 fake clock for reproducible simulation")
    send = commands.add_parser("submit", help="send to local fake-output host")
    send.add_argument("--file", required=True)
    send.add_argument("--socket", type=Path)
    serve = commands.add_parser("serve", help="host fake outputs on a restricted Unix socket")
    serve.add_argument("--socket", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .transport import LocalServer, default_socket
            path = args.socket or default_socket()
            with LocalServer(path) as host:
                print("fake-output server ready (Ctrl-C to stop)", flush=True)
                try:
                    host.serve_forever()
                except KeyboardInterrupt:
                    pass
            path.unlink(missing_ok=True)
            return 0
        raw = _raw(args.file)
        if args.command == "dry-run":
            event = CueEvent.from_json(raw)
            instant = None
            if args.at:
                parsed = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
                    raise ValueError("--at requires a UTC timestamp")
                instant = parsed.astimezone(timezone.utc).timestamp()
            result = Coordinator({channel: FakeSink() for channel in CHANNELS},
                                 clock=(lambda: instant) if instant is not None else None,
                                 policy=QuietPolicy(quiet=args.quiet)).handle(event)
        else:
            from .transport import default_socket, submit
            result = submit(args.socket or default_socket(), raw)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["result"] == "accepted" else 2
    except (ContractError, OSError, RuntimeError, ValueError) as exc:
        # Contract diagnostics include field names only, never notification text.
        print(f"cue error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
