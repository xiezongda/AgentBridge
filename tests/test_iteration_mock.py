"""Offline state-machine tests with real, separate unittest subprocesses."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import run_iteration
from agentbridge.bridge import DEFAULT_AGY, DEFAULT_CODEX, WORKDIR
from agentbridge.iteration import IterationConfig, pause, resume, start, status
from agentbridge.iteration_files import IntegrityError
from agentbridge.process import ProcessResult, run_process

NEON_ID = "neon-session-1"
SOL_ID = "sol-thread-1"
ACCEPTANCE = """\
import unittest
from value import VALUE

class ValueTests(unittest.TestCase):
    def test_value(self):
        self.assertEqual(VALUE, 2, "independent assertion")
"""


def write(path, content):
    path.write_text(content, encoding="utf-8", newline="\n")


def neon(payload, *, conversation=NEON_ID, status="success"):
    return ProcessResult(exit_code=0, stdout=json.dumps({
        "status": status, "conversation_id": conversation, "response": payload,
        "usage": {"input_tokens": 11, "output_tokens": 4},
    }, ensure_ascii=False))


def sol(*, thread=SOL_ID):
    return ProcessResult(exit_code=0, stdout="\n".join(map(json.dumps, [
        {"type": "thread.started", "thread_id": thread},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Edited value.py"}},
        {"type": "turn.completed", "usage": {"input_tokens": 13, "cached_input_tokens": 1,
                                             "output_tokens": 7}},
    ])))


class OfflineRunner:
    def __init__(self, *, values=(2,), decisions=("PASS",)):
        self.values = values
        self.decisions = decisions
        self.calls = {"plan": [], "sol": [], "test": [], "review": []}
        self.hooks = {}

    def __call__(self, argv, **kwargs):
        if argv[0] == str(DEFAULT_AGY):
            role = "review" if "--conversation" in argv else "plan"
        elif argv[0] == str(DEFAULT_CODEX):
            role = "sol"
        elif argv[0] == sys.executable:
            role = "test"
        else:
            raise AssertionError(f"unexpected offline subprocess: {argv[0]}")
        self.calls[role].append((argv, kwargs))
        number = len(self.calls[role])
        if role in self.hooks:
            result = self.hooks[role](number, argv, kwargs)
            if result is not None:
                return result
        if role == "plan":
            return neon({"plan": "Repair the computation and preserve the interface."})
        if role == "sol":
            write(kwargs["cwd"] / "value.py", f"VALUE = {self.values[number - 1]}\n")
            return sol()
        if role == "review":
            decision = self.decisions[number - 1]
            return neon({"decision": decision, "reason": f"review round {number}",
                         "feedback": "Neon guidance: correct the value without bypassing tests."})
        return run_process(argv, **kwargs)

    def counts(self):
        return {role: len(calls) for role, calls in self.calls.items()}


class IterationMockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=WORKDIR, prefix="iteration mock ")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.project = self.folder / "project"
        self.acceptance = self.folder / "acceptance_tests"
        self.output = self.folder / "audit"
        self.project.mkdir()
        self.acceptance.mkdir()
        write(self.project / "value.py", "VALUE = 0\n")
        write(self.acceptance / "test_value.py", ACCEPTANCE)

    def config(self, **overrides):
        return IterationConfig(**{
            "goal": "修正计算结果。", "project_dir": self.project, "tests_dir": self.acceptance,
            "output_dir": self.output, **overrides,
        })

    def test_retry_and_pass_with_exact_arguments_and_evidence(self):
        runner = OfflineRunner(values=(1, 2), decisions=("RETRY", "PASS"))
        write(self.project / "old.txt", "unused")

        def change_files(number, _argv, _kwargs):
            if number == 1:
                (self.project / "old.txt").unlink()
                write(self.project / "new.txt", "added")

        runner.hooks["sol"] = change_files
        state = start(self.config(), runner=runner)
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state)
        self.assertEqual(runner.counts(), {"plan": 1, "sol": 2, "test": 3, "review": 2})
        self.assertEqual(state["total_model_calls"], 5)
        self.assertEqual(state["sol_attempts"], 2)
        self.assertEqual(state["current_round"], 2)
        self.assertEqual(state["tokens"]["sol"]["input_tokens"], 26)
        self.assertEqual(state["tokens"]["neon"]["output_tokens"], 12)
        self.assertEqual(state["tokens"]["combined"]["total_tokens"], 85)
        self.assertFalse(state["baseline_test"]["test_passed"])
        self.assertTrue(state["rounds"]["2"]["test"]["test_passed"])
        self.assertEqual(state["rounds"]["1"]["changes"], {
            "added": ["new.txt"], "deleted": ["old.txt"], "modified": ["value.py"],
        })
        audit = Path(state["run_dir"])
        diff = (audit / "round_1_diff.patch").read_text(encoding="utf-8")
        self.assertIn("--- a/value.py\n+++ b/value.py\n@@", diff)
        self.assertIn("-VALUE = 0\n+VALUE = 1", diff)
        self.assertIn("\\ No newline at end of file", diff)
        self.assertEqual(json.loads((audit / "state.json").read_text(encoding="utf-8")), state)
        events = [json.loads(line) for line in (audit / "events.jsonl").read_text(
            encoding="utf-8").splitlines()]
        self.assertEqual(events[0]["state"], "INIT")
        self.assertEqual(events[-1]["state"], "COMPLETED_PASS")
        self.assertEqual(len([item for item in events if item["event"] == "invocation_started"]), 8)

        agy_args = [str(DEFAULT_AGY), "--model", "gemini-4-neon-high", "--output-format",
                    "json", "--mode", "plan", "--disable-slash-commands", "--print-timeout", "240s"]
        self.assertEqual(runner.calls["plan"][0][0][:-1], agy_args + ["--print"])
        for argv, _ in runner.calls["review"]:
            self.assertEqual(argv[:-1], agy_args + ["--conversation", NEON_ID, "--print"])
        first, second = runner.calls["sol"]
        model = ["-m", "gpt-6.2-sol", "-c", "model_reasoning_effort=high"]
        scope = ["-s", "workspace-write", "-C", str(self.project)]
        tail = ["--skip-git-repo-check", "--json"]
        self.assertEqual(first[0], [str(DEFAULT_CODEX), "exec", *model, *scope, *tail, "-"])
        self.assertEqual(second[0], [str(DEFAULT_CODEX), "exec", *scope, "resume",
                                     *model, *tail, SOL_ID, "-"])
        self.assertTrue(all(arg.isascii() for arg in second[0]))
        self.assertIn("修正计算结果。", first[1]["stdin"].decode("utf-8"))
        for prompt in (second[1]["stdin"].decode("utf-8"), runner.calls["review"][0][0][-1]):
            self.assertIn("independent assertion", prompt)
            self.assertIn("+VALUE = 1", prompt)
            self.assertIn("new.txt", prompt)
        self.assertIn("Neon guidance", second[1]["stdin"].decode("utf-8"))
        for calls in runner.calls.values():
            for _, call in calls:
                self.assertEqual(call["cwd"], self.project)
                self.assertEqual(call["env"]["AGENTBRIDGE_IN_SUBPROCESS"], "1")
                self.assertEqual(call["env"]["AGENTBRIDGE_RUN_ID"], state["run_id"])
        self.assertTrue(all(call["env"]["PYTHONPATH"] == str(self.project)
                            for _, call in runner.calls["test"]))

    def test_pause_and_resume_does_not_replay_completed_steps(self):
        for stage, expected in (("neon_plan", 0), ("sol_exec", 1)):
            with self.subTest(stage=stage):
                write(self.project / "value.py", "VALUE = 0\n")
                runner = OfflineRunner()
                initial = start(self.config(), runner=runner, pause_after=stage)
                self.assertEqual(initial["current_state"], "PAUSED")
                self.assertTrue(initial["resumable"])
                self.assertEqual(len(runner.calls["sol"]), expected)
                self.assertEqual(len(runner.calls["test"]), 1)
                state = resume(Path(initial["run_dir"]), runner=runner)
                self.assertEqual(state["current_state"], "COMPLETED_PASS", state)
                self.assertEqual(runner.counts(), {"plan": 1, "sol": 1, "test": 2, "review": 1})
                self.assertEqual(len(state["completed_steps"]), len(set(state["completed_steps"])))
                counts = runner.counts()
                again = resume(Path(state["run_dir"]), runner=runner)
                self.assertEqual(again, state)
                self.assertEqual(runner.counts(), counts)

    def test_interrupted_in_flight_attempt_is_not_replayed(self):
        class AbruptExit(BaseException):
            pass

        runner = OfflineRunner()

        def interrupt(_number, _argv, _kwargs):
            write(self.project / "value.py", "VALUE = 1\n")
            raise AbruptExit

        runner.hooks["sol"] = interrupt
        with self.assertRaises(AbruptExit):
            start(self.config(), runner=runner)
        run_dir, = self.output.iterdir()
        self.assertEqual(status(run_dir)["pending_step"]["step"], "round_1.sol_exec")
        state = resume(run_dir, runner=runner)
        self.assertEqual(state["current_state"], "STOPPED_NEED_HUMAN")
        self.assertFalse(state["resumable"])
        self.assertEqual(runner.counts(), {"plan": 1, "sol": 1, "test": 1, "review": 0})

    def test_lock_rejects_another_driver(self):
        runner = OfflineRunner()

        def verify_lock(_number, _argv, kwargs):
            run_dir = self.output / kwargs["env"]["AGENTBRIDGE_RUN_ID"]
            with self.assertRaisesRegex(ValueError, "locked"):
                resume(run_dir, runner=runner)

        runner.hooks["sol"] = verify_lock
        self.assertEqual(start(self.config(), runner=runner)["current_state"], "COMPLETED_PASS")

    def test_tamper_after_sol_stops_before_independent_test_or_review(self):
        runner = OfflineRunner()
        write(self.acceptance / "remove.dat", "fixture")

        def tamper(_number, _argv, _kwargs):
            write(self.acceptance / "test_value.py", "pass\n")
            write(self.acceptance / "new.dat", "data")
            (self.acceptance / "remove.dat").unlink()

        runner.hooks["sol"] = tamper
        state = start(self.config(), runner=runner)
        self.assertEqual(state["current_state"], "STOPPED_PERMISSION_OR_TAMPER")
        self.assertTrue(state["tampering_detected"])
        self.assertEqual(state["acceptance_drift"], {
            "added": ["new.dat"], "modified": ["test_value.py"], "deleted": ["remove.dat"],
        })
        self.assertEqual(runner.counts(), {"plan": 1, "sol": 1, "test": 1, "review": 0})

    def test_tamper_during_independent_tests_is_observed_after_the_process(self):
        write(self.acceptance / "test_value.py", ACCEPTANCE + f"""
        from pathlib import Path
        Path({str(self.acceptance / 'new.dat')!r}).write_text("drift", encoding="utf-8")
""")
        runner = OfflineRunner()
        result = start(self.config(), runner=runner)
        self.assertEqual(result["current_state"], "STOPPED_PERMISSION_OR_TAMPER", result)
        self.assertEqual(result["acceptance_drift"]["added"], ["new.dat"])
        self.assertEqual(len(runner.calls["test"]), 2)
        self.assertTrue(result["test_status"]["test_passed"])
        self.assertEqual(len(runner.calls["review"]), 0)

    def test_pause_detects_changes_to_tested_project_before_using_review(self):
        runner = OfflineRunner()
        paused = start(self.config(), runner=runner, pause_after="neon_review")
        write(self.project / "value.py", "VALUE = 0\n")
        state = resume(Path(paused["run_dir"]), runner=runner)
        self.assertEqual(state["current_state"], "STOPPED_PERMISSION_OR_TAMPER")
        self.assertEqual(runner.counts(), {"plan": 1, "sol": 1, "test": 2, "review": 1})

    def test_neon_pass_cannot_override_failing_or_skipped_tests(self):
        cases = (
            ("failure", ACCEPTANCE, 1, "exited with code 1"),
            ("skips", ACCEPTANCE + """
    @unittest.skip("not executed")
    def test_skip(self):
        pass
""", 2, "skipped tests"),
            ("empty", "", 2, "at least 1 executed tests"),
        )
        for label, contents, value, reason in cases:
            with self.subTest(label=label):
                write(self.acceptance / "test_value.py", contents)
                runner = OfflineRunner(values=(value,))
                state = start(self.config(), runner=runner)
                self.assertEqual(state["model_decision"], "PASS")
                self.assertEqual(state["current_state"], "STOPPED_NEED_HUMAN", state)
                self.assertFalse(state["test_status"]["test_passed"])
                self.assertIn(reason, state["override_reason"])

    def test_explicit_skips_still_do_not_count_toward_minimum(self):
        write(self.acceptance / "test_value.py", ACCEPTANCE + """
    @unittest.skip("not executed")
    def test_skip(self):
        pass
""")
        for count, expected in ((1, "COMPLETED_PASS"), (2, "STOPPED_NEED_HUMAN")):
            with self.subTest(min_tests=count):
                state = start(self.config(min_tests=count, allow_skipped=True), runner=OfflineRunner())
                self.assertEqual(state["current_state"], expected, state)
                self.assertEqual(state["test_status"]["skipped"], 1)

    def test_project_cannot_shadow_unittest_or_use_python_startup_hook(self):
        for name in ("unittest.py", "sitecustomize.py"):
            write(self.project / name, "import os\nos._exit(0)\n")
        state = start(self.config(), runner=OfflineRunner(values=(1,)))
        self.assertEqual(state["current_state"], "STOPPED_NEED_HUMAN")
        self.assertEqual(state["test_status"]["process"]["exit_code"], 1)
        self.assertEqual(state["test_status"]["tests_run"], 1)

    def test_forged_unittest_report_cannot_replace_default_runner_completion(self):
        runner = OfflineRunner()

        def fake_success(_number, _argv, _kwargs):
            output = "-" * 70 + "\nRan 1 test in 0.001s\n\nOK\n"
            write(self.project / "value.py", "import os, sys\n"
                  + f"sys.stderr.write({output!r})\nsys.stderr.flush()\nos._exit(0)\n")
            return sol()

        runner.hooks["sol"] = fake_success
        state = start(self.config(), runner=runner)
        self.assertEqual(state["current_state"], "STOPPED_NEED_HUMAN", state)
        self.assertEqual(state["model_decision"], "PASS")
        self.assertIn("unittest completion receipt", state["override_reason"])

    def test_retry_limits_and_hard_caps(self):
        for options, expected, rounds, calls in (
            ({"max_sol_attempts": 1}, "STOPPED_MAX_ATTEMPTS", 1, 3),
            ({"max_model_calls": 2}, "STOPPED_BUDGET_EXCEEDED", 1, 2),
            ({}, "STOPPED_CONSECUTIVE_FAILURES", 2, 5),
            ({"max_consecutive_failures": 3}, "STOPPED_MAX_ATTEMPTS", 3, 7),
        ):
            with self.subTest(options=options):
                runner = OfflineRunner(values=(1, 1, 1), decisions=("RETRY",) * 3)
                state = start(self.config(**options), runner=runner)
                self.assertEqual(state["current_state"], expected, state)
                self.assertEqual(state["current_round"], rounds)
                self.assertEqual(state["total_model_calls"], calls)
                self.assertTrue(state["resumable"])
                saved_calls = runner.counts()
                state = resume(Path(state["run_dir"]), runner=runner)
                self.assertEqual(state["current_state"], expected)
                self.assertEqual(runner.counts(), saved_calls)
        for options in ({"max_sol_attempts": 4}, {"max_model_calls": 8}, {"max_sol_attempts": 0}):
            with self.assertRaises(ValueError):
                self.config(**options)

    def test_increased_budget_resumes_at_saved_stage(self):
        runner = OfflineRunner()
        state = start(self.config(max_model_calls=2), runner=runner)
        self.assertEqual(state["current_state"], "STOPPED_BUDGET_EXCEEDED")
        state = resume(Path(state["run_dir"]), max_model_calls=3, runner=runner)
        self.assertEqual(state["current_state"], "COMPLETED_PASS", state)
        self.assertEqual(runner.counts(), {"plan": 1, "sol": 1, "test": 2, "review": 1})

    def test_total_wall_clock_budget_is_cumulative_across_resume(self):
        now = [0]
        runner = OfflineRunner()

        def consume_budget(number, argv, kwargs):
            if number == 1:
                self.assertLessEqual(kwargs["timeout_s"], 1)
                result = run_process(argv, **kwargs)
                now[0] += 2
                return result

        runner.hooks["test"] = consume_budget
        state = start(self.config(max_total_seconds=1), runner=runner, clock=lambda: now[0])
        self.assertEqual(state["current_state"], "STOPPED_TIMEOUT_BUDGET", state)
        self.assertEqual(state["elapsed_seconds"], 2)
        self.assertTrue(state["resumable"])
        now[0] += 20
        resumed = resume(Path(state["run_dir"]), max_total_seconds=3, runner=runner,
                         clock=lambda: now[0])
        self.assertEqual(resumed["current_state"], "COMPLETED_PASS", resumed)
        self.assertEqual(resumed["elapsed_seconds"], 2)
        self.assertEqual(runner.counts(), {"plan": 1, "sol": 1, "test": 2, "review": 1})

    def test_timeout_limited_by_remaining_budget_stops_with_audit(self):
        runner = OfflineRunner()
        runner.hooks["plan"] = lambda *_: ProcessResult(timed_out=True, stdout="partial")
        state = start(self.config(max_total_seconds=5), runner=runner)
        self.assertEqual(state["current_state"], "STOPPED_TIMEOUT_BUDGET")
        self.assertIsNone(state["pending_step"])
        self.assertTrue(state["resumable"])
        self.assertLessEqual(runner.calls["plan"][0][1]["timeout_s"], 5)

    def test_model_unavailable_is_resumable_and_audited(self):
        for response in (ProcessResult(launch_error="not found"),
                         ProcessResult(exit_code=42, stderr="backend down"),
                         ProcessResult(timed_out=True, stdout="partial"),
                         neon({}, status="unavailable")):
            with self.subTest(response=response):
                runner = OfflineRunner()
                runner.hooks["plan"] = lambda number, *_: response if number == 1 else None
                state = start(self.config(), runner=runner)
                self.assertEqual(state["current_state"], "STOPPED_MODEL_UNAVAILABLE", state)
                self.assertTrue(state["resumable"])
                self.assertIsNone(state["pending_step"])
                audit_file, = Path(state["run_dir"]).glob("*_neon_plan.json")
                audit = json.loads(audit_file.read_text(encoding="utf-8"))
                self.assertEqual(audit["stdout"], response.stdout)
                after = resume(Path(state["run_dir"]), runner=runner)
                self.assertEqual(after["current_state"], "COMPLETED_PASS", after)
                self.assertEqual(runner.counts(), {"plan": 2, "sol": 1, "test": 2, "review": 1})

    def test_model_session_mismatches_and_invalid_utf8_block_pass(self):
        for role, bad_result in (
            ("review", neon({"decision": "PASS", "reason": "looks good"}, conversation="new-session")),
            ("review", ProcessResult(exit_code=0, invalid_utf8_base64={"stdout": "/w=="})),
            ("sol", sol(thread="new-thread")),
        ):
            with self.subTest(role=role, bad_result=bad_result):
                runner = OfflineRunner(values=(1, 2), decisions=("RETRY", "PASS"))
                runner.hooks[role] = lambda number, *_: bad_result if number == 2 else None
                state = start(self.config(), runner=runner)
                self.assertEqual(state["current_state"], "FAILED", state)
                self.assertEqual(state["sol_thread_id"], SOL_ID)
                self.assertEqual(state["neon_conversation_id"], NEON_ID)

    def test_paths_are_isolated(self):
        for directory in (WORKDIR, WORKDIR.parent, WORKDIR / "agentbridge", WORKDIR / "tests",
                          WORKDIR / "runs" / "nested", self.acceptance):
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                self.config(project_dir=directory)
        for options in ({"tests_dir": self.project / "inner"}, {"output_dir": self.project / "audit"},
                        {"tests_dir": WORKDIR.parent}, {"output_dir": WORKDIR / "tests" / "audit"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.config(**options)
        link = self.folder / "alias"
        try:
            link.symlink_to(self.project, target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(IntegrityError):
            start(self.config(project_dir=link), runner=OfflineRunner())

    def test_nested_dispatch_is_refused_before_launch_or_writes(self):
        runner = OfflineRunner()
        with patch.dict(os.environ, {"AGENTBRIDGE_IN_SUBPROCESS": "1"}):
            with self.assertRaisesRegex(ValueError, "nested"):
                start(self.config(), runner=runner)
            error = io.StringIO()
            with redirect_stderr(error), self.assertRaises(SystemExit):
                run_iteration.main(["start", "--help"], runner=runner)
        self.assertIn("nested AgentBridge scheduling is forbidden", error.getvalue())
        self.assertFalse(self.output.exists())
        self.assertFalse(any(runner.counts().values()))

    def test_cli_start_pause_status_resume_with_trusted_custom_command(self):
        runner = OfflineRunner()
        goal_file = self.folder / "goal.txt"
        write(goal_file, "修正计算结果。")

        def cli(*args):
            console = io.StringIO()
            with redirect_stdout(console):
                code = run_iteration.main(list(args), runner=runner)
            return code, json.loads(console.getvalue())

        def request_pause(_number, _argv, kwargs):
            run_dir = str(self.output / kwargs["env"]["AGENTBRIDGE_RUN_ID"])
            code, active = cli("status", "--run-dir", run_dir)
            self.assertEqual((code, active["current_state"]), (0, "NEON_PLAN"))
            code, marker = cli("pause", "--run-dir", run_dir)
            self.assertEqual((code, marker["pause_requested"]), (0, True))

        runner.hooks["plan"] = request_pause
        code, paused = cli("start", "--goal-file", str(goal_file), "--project-dir", str(self.project),
                           "--tests-dir", str(self.acceptance), "--output-dir", str(self.output),
                           "--acceptance-cmd", sys.executable, "-B", "-m", "unittest", "discover",
                           "-s", str(self.acceptance), "-v")
        self.assertEqual((code, paused["current_state"]), (2, "PAUSED"))
        run_dir = paused["run_dir"]
        self.assertFalse((Path(run_dir) / "PAUSE").exists())
        code, completed = cli("resume", "--run-dir", run_dir)
        self.assertEqual((code, completed["current_state"]), (0, "COMPLETED_PASS"))
        code, current = cli("status", "--run-dir", run_dir)
        self.assertEqual(code, 0)
        self.assertEqual(current["neon_conversation_id"], NEON_ID)
        self.assertEqual(current["sol_thread_id"], SOL_ID)
        self.assertTrue(current["test_status"]["test_passed"])
        self.assertEqual(current["total_model_calls"], 3)
        self.assertFalse(pause(Path(run_dir))["pause_requested"])

    def test_example_starts_with_three_reproducible_failures(self):
        example = WORKDIR / "examples" / "mini_ledger"
        result = run_process([
            sys.executable, "-B", "-m", "unittest", "discover",
            "-s", str(example / "acceptance_tests"), "-v",
        ], cwd=WORKDIR, timeout_s=10, env=dict(
            os.environ, PYTHONPATH=str(example / "project"), PYTHONIOENCODING="utf-8",
        ))
        self.assertEqual(result.exit_code, 1, result.stderr)
        self.assertTrue(result.utf8_ok)
        self.assertIn("Ran 8 tests", result.stderr)
        self.assertIn("FAILED (failures=3)", result.stderr)
        for name in ("test_refund_reduces_net_and_fee", "test_discount_applies_at_threshold",
                     "test_transaction_id_comparison_ignores_case_and_outer_whitespace"):
            self.assertIn(f"FAIL: {name}", result.stderr)
