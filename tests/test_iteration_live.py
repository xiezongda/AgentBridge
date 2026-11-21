"""Run the demo on temporary copies after the coordinator opts in."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from agentbridge.bridge import WORKDIR
from agentbridge.iteration import IterationConfig, start


@unittest.skipUnless(os.environ.get("AGENTBRIDGE_RUN_LIVE_TESTS") == "1", "live model calls are opt-in")
class IterationLiveTests(unittest.TestCase):
    def test_independent_ledger_acceptance(self):
        example = WORKDIR / "examples" / "mini_ledger"
        with tempfile.TemporaryDirectory(dir=WORKDIR, prefix="iteration_live_") as temp:
            folder = Path(temp)
            project = folder / "project"
            tests = folder / "acceptance_tests"
            ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
            shutil.copytree(example / "project", project, ignore=ignore)
            shutil.copytree(example / "acceptance_tests", tests, ignore=ignore)
            state = start(IterationConfig(
                goal=(example / "goal.txt").read_text(encoding="utf-8"),
                project_dir=project, tests_dir=tests, min_tests=8,
                max_consecutive_failures=3,
            ))
            self.assertFalse(state["baseline_test"]["test_passed"], state["run_dir"])
            self.assertEqual(state["current_state"], "COMPLETED_PASS", state["run_dir"])
            self.assertTrue(state["test_status"]["test_passed"], state["run_dir"])
            self.assertEqual(state["test_status"]["tests_run"], 8, state["run_dir"])
