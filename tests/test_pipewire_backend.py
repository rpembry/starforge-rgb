"""No PipeWire command or sound is invoked by this suite."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import unittest

from starforge_cues import Coordinator, CueEvent, FakeSink
from starforge_cues.audio import AudioAdapter, original_test_earcon
from starforge_cues.pipewire_backend import PipeWireBackend

SINK = "sink.selected"
OTHER = "sink.other"


def node(node_id, name, kind, serial):
    return {"id": node_id, "type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": name, "media.class": kind,
                               "object.serial": serial}}}


def link(source, target):
    return {"type": "PipeWire:Interface:Link",
            "info": {"output-node-id": source, "input-node-id": target}}


class Process:
    def __init__(self):
        self.alive = True
        self.terminations = 0
        self.kill_count = 0
        self.wait_timeout = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminations += 1
        if not self.wait_timeout:
            self.alive = False

    def kill(self):
        self.kill_count += 1
        self.alive = False

    def wait(self, timeout):
        if self.alive:
            raise subprocess.TimeoutExpired("pw-play", timeout)
        return 0


class Rig:
    def __init__(self):
        self.now = 0.0
        self.processes = []
        self.commands = []
        self.feeds = []
        self.link_target = 10
        self.stream_visible = True
        self.sink_visible = True
        self.graph_fails = False
        self.raise_after_feed = False

    def snapshot(self):
        if self.graph_fails:
            raise RuntimeError("graph unavailable")
        graph = [node(10, SINK, "Audio/Sink", 67),
                 node(11, OTHER, "Audio/Sink", 68)] if self.sink_visible else [node(11, OTHER, "Audio/Sink", 68)]
        if self.processes and self.processes[-1].alive and self.stream_visible:
            name = self.commands[-1][self.commands[-1].index("--properties") + 1]
            stream = node(20, json.loads(name)["node.name"], "Stream/Output/Audio", 90)
            stream["info"]["props"].update(json.loads(name))
            graph.extend([stream, link(20, self.link_target)])
        return graph

    def spawn(self, args, **kwargs):
        self.commands.append(args)
        self.processes.append(Process())
        assert kwargs["shell"] is False
        assert kwargs["start_new_session"] is True
        return self.processes[-1]

    def feed(self, process, pcm, check, deadline, clock):
        check()
        self.feeds.append(pcm)
        if self.raise_after_feed:
            raise RuntimeError("lost acknowledgment")

    def sleep(self, seconds):
        self.now += seconds

    def backend(self, commissioning_override=False):
        return PipeWireBackend(snapshot=self.snapshot, spawn=self.spawn,
                               feed=self.feed, clock=lambda: self.now, sleep=self.sleep,
                               commissioning_override=commissioning_override)


def plan(cue="job.completed"):
    return {"operation": "cue", "cue_id": cue}


class PipeWireTests(unittest.TestCase):
    def setup_adapter(self, rig=None):
        rig = rig or Rig()
        backend = rig.backend()
        adapter = AudioAdapter(backend, SINK, {"job.completed": original_test_earcon()},
                               monotonic_clock=lambda: rig.now)
        return rig, backend, adapter

    def test_exact_target_gain_count_and_no_system_volume_command(self):
        rig, backend, adapter = self.setup_adapter()
        self.assertEqual(adapter.dispatch("audio", plan()), "accepted")
        args = rig.commands[0]
        self.assertEqual(args[args.index("--target") + 1], "67")
        self.assertEqual(args[args.index("--volume") + 1], "0.05")
        self.assertEqual(args[args.index("--sample-count") + 1], "12000")
        self.assertEqual(args[args.index("--latency") + 1], "20ms")
        props = json.loads(args[args.index("--properties") + 1])
        self.assertEqual(props["target.object"], "67")
        self.assertTrue(all(props[key] is True for key in
                            ("node.dont-fallback", "node.dont-move", "node.dont-reconnect")))
        self.assertEqual(args[-1], "-")
        self.assertEqual(len(rig.feeds[0]), 24000)
        self.assertEqual(adapter.dispatch("audio", {"operation": "clear"}), "accepted")
        self.assertEqual(rig.processes[0].terminations, 1)

    def test_15_percent_requires_independent_adapter_and_backend_opt_in(self):
        clip = original_test_earcon()
        rig = Rig()
        backend = rig.backend()
        adapter = AudioAdapter(backend, SINK, {"job.completed": clip}, gain=0.15,
                               commissioning_override=True)
        self.assertEqual(adapter.dispatch("audio", plan()), "unknown")
        self.assertEqual(rig.commands, [])

        rig = Rig()
        backend = rig.backend(commissioning_override=True)
        with self.assertRaises(ValueError):
            AudioAdapter(backend, SINK, {"job.completed": clip}, gain=0.15)
        with self.assertRaises(ValueError):
            AudioAdapter(backend, SINK, {"job.completed": clip}, gain=0.1501,
                         commissioning_override=True)
        adapter = AudioAdapter(backend, SINK, {"job.completed": clip}, gain=0.15,
                               commissioning_override=True)
        self.assertEqual(adapter.dispatch("audio", plan()), "accepted")
        args = rig.commands[0]
        self.assertEqual(args[args.index("--volume") + 1], "0.15")
        self.assertEqual(args[args.index("--target") + 1], "67")
        self.assertEqual(len(rig.feeds), 1)
        self.assertEqual(adapter.dispatch("audio", {"operation": "clear"}), "accepted")

    def test_backend_direct_gain_bounds_and_asset_checks_before_spawn(self):
        rig = Rig()
        backend = rig.backend(commissioning_override=True)
        clip = original_test_earcon()
        for gain in (0.1501, float("nan"), -0.01):
            with self.assertRaises(ValueError):
                backend.start(SINK, clip.wav, gain)
        with self.assertRaises(ValueError):
            backend.start(SINK, b"invalid", 0.15)
        self.assertEqual(rig.commands, [])

    def test_missing_or_wrong_target_never_receives_pcm(self):
        rig, backend, adapter = self.setup_adapter()
        rig.sink_visible = False
        self.assertEqual(adapter.dispatch("audio", plan()), "unsupported")
        self.assertEqual(rig.commands, [])
        rig.sink_visible = True
        rig.link_target = 11
        self.assertEqual(adapter.dispatch("audio", plan()), "unknown")
        self.assertEqual(rig.feeds, [])
        self.assertEqual(len(rig.commands), 1)
        self.assertFalse(rig.processes[0].alive)

    def test_delayed_stream_cannot_clear_by_elapsed_clip_time(self):
        rig, backend, adapter = self.setup_adapter()
        rig.raise_after_feed = True
        # Graph becomes unavailable after the start acknowledgment is lost.
        def lost_ack(process, pcm, check, deadline, clock):
            check()
            rig.feeds.append(pcm)
            rig.graph_fails = True
            raise RuntimeError("receipt lost")
        backend.feed = lost_ack
        self.assertEqual(adapter.dispatch("audio", plan()), "unknown")
        rig.now += 5.0
        self.assertEqual(adapter.dispatch("audio", plan()), "unknown")
        self.assertEqual(len(rig.commands), 1)
        rig.graph_fails = False
        rig.raise_after_feed = False
        backend.feed = rig.feed
        self.assertEqual(adapter.dispatch("audio", plan()), "accepted")
        self.assertEqual(len(rig.commands), 2)

    def test_stop_timeout_and_graph_uncertainty_block_replacement(self):
        rig, backend, adapter = self.setup_adapter()
        self.assertEqual(adapter.dispatch("audio", plan()), "accepted")
        rig.processes[0].wait_timeout = True
        rig.graph_fails = True
        self.assertEqual(adapter.dispatch("audio", plan()), "unknown")
        self.assertEqual(len(rig.commands), 1)
        rig.graph_fails = False
        self.assertEqual(adapter.dispatch("audio", plan()), "accepted")
        self.assertEqual(rig.processes[0].kill_count, 1)
        self.assertEqual(len(rig.commands), 2)

    def test_unmapped_replacement_stops_owned_process(self):
        rig, backend, adapter = self.setup_adapter()
        self.assertEqual(adapter.dispatch("audio", plan()), "accepted")
        self.assertEqual(adapter.dispatch("audio", plan("unmapped")), "unsupported")
        self.assertFalse(rig.processes[0].alive)
        self.assertEqual(len(rig.commands), 1)

    def test_graph_changes_before_feed_prevent_pcm(self):
        rig, backend, adapter = self.setup_adapter()
        def reroute(process, pcm, check, deadline, clock):
            rig.link_target = 11
            check()
            rig.feeds.append(pcm)
        backend.feed = reroute
        self.assertEqual(adapter.dispatch("audio", plan()), "unknown")
        self.assertEqual(rig.feeds, [])
        self.assertFalse(rig.processes[0].alive)

    def test_reroute_after_feeder_check_is_unknown_and_stopped(self):
        rig, backend, adapter = self.setup_adapter()
        def reroute_after_queue(process, pcm, check, deadline, clock):
            check()  # route was correct before PCM was queued
            rig.feeds.append(pcm)
            rig.link_target = 11
        backend.feed = reroute_after_queue
        self.assertEqual(adapter.dispatch("audio", plan()), "unknown")
        self.assertFalse(rig.processes[0].alive)
        self.assertEqual(len(rig.commands), 1)

    def test_missing_target_after_feed_and_extra_link_are_unknown(self):
        for change in ("missing", "extra"):
            with self.subTest(change=change):
                rig, backend, adapter = self.setup_adapter()
                original_snapshot = rig.snapshot
                extra = [False]
                def snapshot():
                    graph = original_snapshot()
                    if extra[0]:
                        if change == "missing":
                            graph = [obj for obj in graph if obj.get("id") != 10]
                        else:
                            graph.append(link(20, 11))
                    return graph
                backend.snapshot = snapshot
                def change_after_queue(process, pcm, check, deadline, clock):
                    check()
                    rig.feeds.append(pcm)
                    extra[0] = True
                backend.feed = change_after_queue
                self.assertEqual(adapter.dispatch("audio", plan()), "unknown")
                self.assertFalse(rig.processes[0].alive)

    def test_unverified_routing_properties_withhold_pcm(self):
        rig, backend, adapter = self.setup_adapter()
        original_snapshot = rig.snapshot
        def snapshot():
            graph = original_snapshot()
            for obj in graph:
                if obj.get("id") == 20:
                    obj["info"]["props"].pop("node.dont-fallback")
            return graph
        backend.snapshot = snapshot
        self.assertEqual(adapter.dispatch("audio", plan()), "unknown")
        self.assertEqual(rig.feeds, [])
        self.assertFalse(rig.processes[0].alive)

    def test_graph_failure_is_unknown_while_rgb_remains_independent(self):
        rig, backend, adapter = self.setup_adapter()
        rig.link_target = 11
        fixture = json.loads((Path(__file__).resolve().parents[1] /
                              "examples/synthetic-cue.json").read_text())
        stamp = datetime(2026, 10, 3, tzinfo=timezone.utc).isoformat()
        event = CueEvent.from_mapping({**fixture, "occurred_at": stamp,
                                       "observed_at": stamp})
        core = Coordinator({"audio": adapter, "rgb": FakeSink()},
                           clock=lambda: datetime.fromisoformat(stamp).timestamp())
        result = core.handle(event)
        self.assertEqual(result["channels"]["audio"], "unknown")
        self.assertEqual(result["channels"]["rgb"], "accepted")
        self.assertEqual(rig.feeds, [])


if __name__ == "__main__":
    unittest.main()
