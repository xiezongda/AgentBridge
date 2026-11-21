"""One live handoff when the coordinator explicitly enables it."""

import os
import unittest

from agentbridge.bridge import BridgeConfig, run_loop


@unittest.skipUnless(os.environ.get("AGENTBRIDGE_RUN_LIVE_TESTS") == "1", "live model calls are opt-in")
class BridgeLiveTests(unittest.TestCase):
    def test_one_live_handoff(self):
        summary = run_loop(BridgeConfig())
        self.assertEqual(len(summary["calls"]), 3, summary)
        self.assertEqual(summary["final_decision"], "PASS", summary)
