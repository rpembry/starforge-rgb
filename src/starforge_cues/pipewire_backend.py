"""Opt-in PipeWire playback backend. Importing this module has no audio effect.

A selected node name is resolved to a fresh sink serial. PCM is withheld until
an owned stream has an observed link to that serial and no other sink. Process
exit and disappearance of the owned graph node/link are required before reuse.
PipeWire or device buffers beyond that boundary cannot be proven empty here.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
import uuid
import wave
import io
from typing import Callable

from .audio import MAX_GAIN, MAX_WAV_BYTES, PlaybackReceipt, _SINK_ID


class GraphError(RuntimeError):
    pass


def _nodes(graph: list[dict]) -> dict[int, dict]:
    return {obj["id"]: obj.get("info", {}).get("props", {}) for obj in graph
            if obj.get("type") == "PipeWire:Interface:Node" and type(obj.get("id")) is int}


def _links(graph: list[dict]) -> list[tuple[int, int]]:
    result = []
    for obj in graph:
        if obj.get("type") == "PipeWire:Interface:Link":
            info = obj.get("info", {})
            out, target = info.get("output-node-id"), info.get("input-node-id")
            if type(out) is int and type(target) is int:
                result.append((out, target))
    return result


def _one_sink(graph: list[dict], name: str) -> tuple[int, int] | None:
    found = [(node_id, props.get("object.serial")) for node_id, props in _nodes(graph).items()
             if props.get("media.class") == "Audio/Sink" and props.get("node.name") == name]
    if len(found) != 1 or type(found[0][1]) is not int or found[0][1] <= 0:
        return None
    return found[0]


def _owned_graph_state(graph: list[dict], stream_name: str,
                       sink_id: int, serial: int) -> tuple[bool, bool]:
    """Return (owned node present, exclusively linked to selected sink)."""
    owned = [(node_id, props) for node_id, props in _nodes(graph).items()
             if props.get("node.name") == stream_name]
    if len(owned) != 1:
        return bool(owned), False
    node_id, props = owned[0]
    # Do not trust flags solely because we requested them: verify the stream
    # actually exposes the routing controls before supplying any PCM.
    pinned = (str(props.get("target.object")) == str(serial) and
              all(props.get(key) in (True, "true") for key in
                  ("node.dont-fallback", "node.dont-move", "node.dont-reconnect")))
    outgoing = [target for source, target in _links(graph) if source == node_id]
    return True, pinned and bool(outgoing) and all(target == sink_id for target in outgoing)


def _snapshot() -> list[dict]:
    result = subprocess.run(["pw-dump"], capture_output=True, check=True, timeout=1)
    graph = json.loads(result.stdout)
    if not isinstance(graph, list):
        raise GraphError("invalid PipeWire graph")
    return graph


def _feed_pcm(process, pcm: bytes, check: Callable[[], None], deadline: float,
              clock: Callable[[], float]) -> None:
    """Bound writes, checking the graph before every chunk; no shell involved."""
    fd = process.stdin.fileno()
    os.set_blocking(fd, False)
    offset = 0
    while offset < len(pcm):
        check()
        if process.poll() is not None or clock() >= deadline:
            raise GraphError("stream exited or feed deadline elapsed")
        if not select.select([], [fd], [], min(0.02, max(0, deadline - clock())))[1]:
            continue
        try:
            offset += os.write(fd, pcm[offset:offset + 4096])
        except BlockingIOError:
            continue
        check()
    process.stdin.close()


class PipeWireBackend:
    """One owned pw-play process, exact target, per-stream gain, fail closed.

    `snapshot`, `spawn`, and `feed` permit tests with fake processes/graphs.
    The default functions are only used when `start` is explicitly called.
    """

    def __init__(self, *, snapshot: Callable[[], list[dict]] = _snapshot,
                 spawn: Callable = subprocess.Popen, feed: Callable = _feed_pcm,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.snapshot, self.spawn, self.feed = snapshot, spawn, feed
        self.clock, self.sleep = clock, sleep
        self._owned = None  # process, stream name, target node id, token, stream node id

    def available_sinks(self) -> frozenset[str]:
        try:
            return frozenset(props["node.name"] for props in _nodes(self.snapshot()).values()
                             if props.get("media.class") == "Audio/Sink" and
                             isinstance(props.get("node.name"), str) and
                             _SINK_ID.fullmatch(props["node.name"]))
        except Exception:
            return frozenset()

    def _clean(self) -> bool:
        if self._owned is None:
            return True
        process, name, _sink_id, _token, stream_id = self._owned
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=0.2)
            # Two spaced, successful snapshots after process reaping. Missing
            # or malformed graph data is uncertainty, never clearance.
            for index in range(2):
                graph = self.snapshot()
                if any(props.get("node.name") == name for props in _nodes(graph).values()):
                    return False
                if stream_id is not None and any(source == stream_id or target == stream_id
                                                  for source, target in _links(graph)):
                    return False
                if index == 0:
                    self.sleep(0.05)
            self._owned = None
            return True
        except Exception:
            return False

    def quiesce_unknown(self) -> str:
        return "accepted" if self._clean() else "unknown"

    def stop(self, sink_id: str, token: str, fade_ms: int) -> str:
        # Termination is prompt and bounded; pw-play has no verified fade API.
        if self._owned is None:
            return "accepted"
        if self._owned[3] != token or fade_ms < 0 or fade_ms > 50:
            return "unknown"
        return self.quiesce_unknown()

    def start(self, sink_id: str, wav: bytes, gain: float) -> PlaybackReceipt:
        if self._owned is not None and not self._clean():
            return PlaybackReceipt("unknown", None)
        if not isinstance(sink_id, str) or not _SINK_ID.fullmatch(sink_id):
            raise ValueError("invalid exact sink")
        if type(gain) not in (float, int) or not 0 <= gain <= MAX_GAIN:
            raise ValueError("invalid gain")
        if not isinstance(wav, bytes) or not 44 <= len(wav) <= MAX_WAV_BYTES:
            raise ValueError("invalid bounded WAV")
        with wave.open(io.BytesIO(wav), "rb") as reader:
            if (reader.getcomptype() != "NONE" or reader.getsampwidth() != 2 or
                    reader.getnchannels() not in (1, 2) or
                    not 8000 <= reader.getframerate() <= 48000 or
                    not 1 <= reader.getnframes() <= reader.getframerate() // 2):
                raise ValueError("invalid bounded PCM")
            rate, channels, frames = (reader.getframerate(), reader.getnchannels(),
                                      reader.getnframes())
            pcm = reader.readframes(frames)
        if len(pcm) != frames * channels * 2:
            raise ValueError("truncated PCM")
        sink = _one_sink(self.snapshot(), sink_id)
        if sink is None:
            raise GraphError("selected sink unavailable or ambiguous")
        sink_node_id, serial = sink
        stream_name = "starforge-cues-" + uuid.uuid4().hex
        token = uuid.uuid4().hex
        args = ["pw-play", "--target", str(serial), "--volume", str(float(gain)),
                "--raw", "--rate", str(rate), "--channels", str(channels),
                "--format", "s16", "--sample-count", str(frames),
                "--latency", "20ms", "--properties",
                json.dumps({"node.name": stream_name, "target.object": str(serial),
                            "node.dont-fallback": True, "node.dont-move": True,
                            "node.dont-reconnect": True}), "-"]
        process = self.spawn(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, shell=False, start_new_session=True)
        # Own it before any further operation can fail or lose acknowledgment.
        self._owned = (process, stream_name, sink_node_id, token, None)
        deadline = self.clock() + 0.5

        def check_link() -> None:
            graph = self.snapshot()
            if _one_sink(graph, sink_id) != sink:
                raise GraphError("selected sink identity changed")
            present, exact = _owned_graph_state(graph, stream_name, sink_node_id, serial)
            if not present or not exact:
                raise GraphError("owned stream lacks exclusive selected-sink link")
            stream_id = next(node_id for node_id, props in _nodes(graph).items()
                             if props.get("node.name") == stream_name)
            self._owned = (process, stream_name, sink_node_id, token, stream_id)

        try:
            while self.clock() < deadline:
                try:
                    check_link()
                    break
                except GraphError:
                    if process.poll() is not None:
                        raise
                    self.sleep(0.01)
            else:
                raise GraphError("exact target link not observed")
            self.feed(process, pcm, check_link, self.clock() + 1.0, self.clock)
            # A route can change after the final feeder check. A mismatch after
            # PCM was queued is an unknown outcome and stops the owned stream.
            check_link()
            return PlaybackReceipt("accepted", token)
        except Exception:
            self._clean()
            return PlaybackReceipt("unknown", token if self._owned is not None else None)
