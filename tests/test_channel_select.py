"""Pure channel choices from fake lease snapshots; no output calls."""

import unittest

from starforge_cues.channel_select import select_channels
from starforge_cues.lease_book import Lease


NOW = 1000.0
LOW = ("source.low", "subject:low")
HIGH = ("source.high", "subject:high")


def lease(priority: int, sequence: int, text: str | None, expiry: float = NOW + 60):
    return Lease(expiry, NOW, 0, priority, sequence,
                 {"subject_id": f"run-{sequence}", "status": "progress", "text": text})


class ChannelSelectionTests(unittest.TestCase):
    def test_bodyless_rgb_winner_does_not_erase_lower_text_or_audio(self):
        leases = {LOW: lease(2, 1, "Lower warning"),
                  HIGH: lease(3, 2, None)}
        selected = select_channels(leases, NOW, text_permitted=True, unlocked=True,
                                   rgb_permitted=True, audio_permitted=True,
                                   admitted_key=LOW, admitted_status="failed",
                                   previous_status="progress")
        self.assertEqual(selected.rgb_key, HIGH)
        self.assertEqual(selected.text_keys, (HIGH, LOW))
        self.assertEqual(leases[selected.text_keys[1]].plan["text"], "Lower warning")
        self.assertEqual(selected.audio_key, LOW)

    def test_channel_specific_quiet_and_fail_closed_text_lock(self):
        leases = {LOW: lease(2, 1, "Visible")}
        locked = select_channels(leases, NOW, text_permitted=True, unlocked=False,
                                 rgb_permitted=True, audio_permitted=False,
                                 admitted_key=LOW, admitted_status="failed")
        self.assertEqual(locked.text_keys, ())
        self.assertEqual(locked.rgb_key, LOW)
        self.assertIsNone(locked.audio_key)
        rgb_muted = select_channels(leases, NOW, text_permitted=False, unlocked=True,
                                    rgb_permitted=False, audio_permitted=True,
                                    admitted_key=LOW, admitted_status="failed")
        self.assertEqual(rgb_muted.text_keys, ())
        self.assertIsNone(rgb_muted.rgb_key)
        self.assertEqual(rgb_muted.audio_key, LOW)

    def test_decision_refresh_and_expired_lease_never_queue_audio(self):
        leases = {LOW: lease(2, 1, "Decision")}
        refreshed = select_channels(leases, NOW, text_permitted=True, unlocked=True,
                                    rgb_permitted=True, audio_permitted=True,
                                    admitted_key=LOW, admitted_status="needs_attention",
                                    previous_status="needs_attention")
        self.assertIsNone(refreshed.audio_key)
        transitioned = select_channels(leases, NOW, text_permitted=True, unlocked=True,
                                       rgb_permitted=True, audio_permitted=True,
                                       admitted_key=LOW, admitted_status="succeeded",
                                       previous_status="needs_attention")
        self.assertEqual(transitioned.audio_key, LOW)
        expired = select_channels(leases, NOW + 61, text_permitted=True, unlocked=True,
                                  rgb_permitted=True, audio_permitted=True,
                                  admitted_key=LOW, admitted_status="succeeded")
        self.assertEqual(expired.text_keys, ())
        self.assertIsNone(expired.rgb_key)
        self.assertIsNone(expired.audio_key)
        self.assertEqual(leases[LOW].plan["text"], "Decision")


if __name__ == "__main__":
    unittest.main()
