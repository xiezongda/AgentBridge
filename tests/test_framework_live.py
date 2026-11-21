"""Opt-in three-role scientific run on a temporary Lennard-Jones copy."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from agentbridge.bridge import WORKDIR
from agentbridge.framework import start
from agentbridge.project_spec import ProjectSpec


@unittest.skipUnless(os.environ.get("AGENTBRIDGE_RUN_LIVE_TESTS") == "1", "live model calls are opt-in")
class FrameworkLiveTests(unittest.TestCase):
    def test_independent_lj_scientific_acceptance(self):
        with tempfile.TemporaryDirectory(dir=WORKDIR, prefix="framework_live_") as temp:
            example = Path(temp) / "lj_physics"
            shutil.copytree(WORKDIR / "examples" / "lj_physics", example,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            spec = ProjectSpec.from_file(example / "project.json")
            state = start(spec)
            self.assertEqual(state["baseline"]["failed_verifiers"], ["lj_tests", "physics_thresholds"],
                             state["run_dir"])
            self.assertEqual(state["current_state"], "COMPLETED_PASS", state["run_dir"])
            self.assertTrue(state["verification"]["all_passed"], state["run_dir"])
            current = state["rounds"][str(state["current_round"])]
            self.assertEqual(current["astra_acceptance"]["decision"], "PASS", state["run_dir"])
            self.assertEqual({v["status"] for v in current["astra_acceptance"]["claim_verdicts"]},
                             {"SUPPORTED"}, state["run_dir"])
