"""Offline tests. The only real subprocesses are local test fixtures."""

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import run_bridge_loop
from agentbridge.bridge import WORKDIR, BridgeConfig, run_loop
from agentbridge.process import ProcessResult, run_process
from agentbridge.protocol import parse_agy_json, parse_codex_jsonl, parse_model_object
from agentbridge.verify import Stage, verify_loop


TASK = {
    "task_id": "中文-01",
    "title": "求和",
    "instruction": "计算 2 + 3，将结果写入 sum。用中文简要说明。",
    "required_output_keys": ["task_id", "sum"],
    "exact_checks": {"sum": 5},
    "must_contain": ["相加"],
    "acceptance_criteria": "返回整数与简要说明。",
}
ANSWER = {"task_id": TASK["task_id"], "sum": 5}
REVIEW = {"decision": "PASS", "reason": "答案正确", "checked_items": ["求和"], "issues": []}
CONVERSATION_ID = "neon-session-1"
THREAD_ID = "sol-thread-1"


def json_text(value):
    return json.dumps(value, ensure_ascii=False)


def agy_stdout(payload, *, conversation=CONVERSATION_ID, status="success"):
    return json_text({
        "status": status,
        "conversation_id": conversation,
        "response": f"```json\n{json_text(payload)}\n```",
        "usage": {"tokens": 8},
        "duration_seconds": 0.1,
    })


def codex_stdout(answer=ANSWER, *, message=None, completed=True):
    if message is None:
        message = f"```json\n{json_text(answer)}\n```\n将 2 和 3 相加。"
    events = [
        {"type": "thread.started", "thread_id": THREAD_ID},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "older", "type": "agent_message", "text": "处理中"}},
        {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": message}},
    ]
    if completed:
        events.append({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 7}})
    return "\r\n".join(map(json_text, events)) + "\r\n"


def completed(stdout, *, code=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout=stdout.encode("utf-8"), stderr=stderr.encode("utf-8")
    )


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class ProtocolTests(unittest.TestCase):
    def test_process_utf8_round_trip(self):
        message = "中文任务，UTF-8 正常 ✓"
        result = run_process(
            [sys.executable, "-c", "import sys; data = sys.stdin.buffer.read(); "
             "sys.stdout.buffer.write(data); sys.stderr.buffer.write(data)"],
            cwd=WORKDIR,
            timeout_s=10,
            stdin=message.encode("utf-8"),
        )
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.utf8_ok)
        self.assertEqual(result.stdout, message)
        self.assertEqual(result.stderr, message)

    @unittest.skipUnless(os.name == "nt", "Windows batch fixture")
    def test_windows_cmd_preserves_binary_stdin(self):
        with tempfile.TemporaryDirectory(dir=WORKDIR, prefix="bridge cmd ") as temp:
            shim = Path(temp) / "mock codex.cmd"
            shim.write_text(
                f'@echo off\n"{sys.executable}" -c "import sys; '
                'sys.stdout.buffer.write(sys.stdin.buffer.read())"\n',
                encoding="utf-8",
            )
            result = run_process(
                [str(shim), "exec", "-"], cwd=WORKDIR, timeout_s=10,
                stdin="中文提示词".encode("utf-8"),
            )
        self.assertEqual(result.exit_code, 0, result.audit_metadata())
        self.assertEqual(result.stdout, "中文提示词")

    def test_agy_json_plain_and_noise_wrapped(self):
        stdout = agy_stdout(TASK)
        for text in (stdout, f'[INFO] 初始化\n{stdout}\n[INFO] 完成'):
            with self.subTest(text=text):
                envelope = parse_agy_json(text)
                self.assertEqual(envelope["conversation_id"], CONVERSATION_ID)
                self.assertEqual(parse_model_object(envelope["response"]), TASK)

    def test_agy_json_rejects_malformed_or_ambiguous_data(self):
        invalid = (
            "没有 JSON", '{"status":', '[{"status":"success"}]',
            '{} {}', '{"status":"success","status":"error"}',
            '{"foo":NaN}', '{"foo":1e999}', '{"foo":"\\ud800"}',
            'log\n{"malformed": {"status":"success"}\n',
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_agy_json(text)

    def test_codex_jsonl_returns_final_message_and_reports_invalid_lines(self):
        stdout = codex_stdout()
        parsed = parse_codex_jsonl(stdout)
        self.assertEqual(parsed["thread_id"], THREAD_ID)
        self.assertEqual(parsed["usage"], {"input_tokens": 12, "output_tokens": 7})
        self.assertEqual(parsed["completed_turns"], 1)
        self.assertEqual(parsed["parse_errors"], [])
        self.assertEqual(parse_model_object(parsed["agent_message"]), ANSWER)
        malformed = parse_codex_jsonl(stdout + '\n噪声\n{"thread_id":3}\n')
        self.assertEqual(len(malformed["parse_errors"]), 2)
        self.assertEqual(malformed["agent_message"], parsed["agent_message"])


class BridgeMockTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=WORKDIR, prefix="bridge_mock_")
        self.addCleanup(temp.cleanup)
        self.config = BridgeConfig(output_dir=Path(temp.name))

    def _run(self, *, task=None, sol=None, review=None):
        responses = [
            completed(agy_stdout(TASK), stderr="任务诊断") if task is None else task,
            completed(codex_stdout(), stderr="执行诊断") if sol is None else sol,
            completed(agy_stdout(REVIEW), stderr="审查诊断") if review is None else review,
        ]
        with patch("agentbridge.process.subprocess.run", side_effect=responses) as mocked:
            summary = run_loop(self.config)
        decision = read_json(summary["decision_record_path"])
        return summary, decision, mocked

    def _assert_override(self, summary, decision, failed_check):
        self.assertEqual(decision["model_decision"], "PASS")
        self.assertFalse(decision["program_passed"])
        self.assertEqual(decision["final_decision"], "FAILED")
        failed = {check["name"]: check["reason"] for check in decision["independent_checks"]
                  if not check["passed"]}
        self.assertIn(failed_check, failed)
        self.assertTrue(failed[failed_check])
        self.assertIn(failed_check, decision["override_reason"])
        self.assertNotEqual(summary["exit_code"], 0)

    def test_full_success_with_utf8_audit_and_exact_cli_arguments(self):
        summary, decision, mocked = self._run()
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(decision["call_count"], 3)
        self.assertEqual(decision["model_decision"], "PASS")
        self.assertEqual(decision["final_decision"], "PASS")
        self.assertTrue(decision["program_passed"])
        self.assertTrue(all(item["passed"] for item in decision["independent_checks"]))
        self.assertEqual(summary["exit_code"], 0)

        task_call, codex_call, review_call = mocked.call_args_list
        for call in mocked.call_args_list:
            self.assertIs(call.kwargs["shell"], False)
            self.assertTrue(call.kwargs["capture_output"])
            self.assertEqual(call.kwargs["cwd"], WORKDIR)
            self.assertEqual(call.kwargs["timeout"], 180)
        agy_args = [str(self.config.agy_path), "--model", "gemini-4-neon-high", "--output-format",
                    "json", "--mode", "plan", "--disable-slash-commands", "--print-timeout", "180s"]
        self.assertEqual(task_call.args[0][:-1], agy_args + ["--print"])
        self.assertEqual(review_call.args[0][:-1], agy_args + ["--conversation", CONVERSATION_ID, "--print"])
        self.assertEqual(codex_call.args[0], [
            str(self.config.codex_path), "exec", "-m", "gpt-6.2-sol", "-c",
            "model_reasoning_effort=medium", "-s", "read-only", "-C", str(WORKDIR),
            "--skip-git-repo-check", "--json", "-",
        ])
        self.assertTrue(all(arg.isascii() for arg in codex_call.args[0]))
        self.assertIsNone(task_call.kwargs["input"])
        sol_prompt = codex_call.kwargs["input"].decode("utf-8")
        self.assertIn(TASK["instruction"], sol_prompt)
        self.assertNotIn('"exact_checks"', sol_prompt)
        review_prompt = review_call.args[0][-1]
        evidence = json.loads(review_prompt.split("\n\n", 1)[1])
        self.assertEqual(evidence["sol_agent_message"], parse_codex_jsonl(codex_stdout())["agent_message"])
        self.assertEqual(evidence["sol_exit_code"], 0)
        self.assertEqual(evidence["sol_thread_id"], THREAD_ID)
        self.assertTrue(all(item["passed"] for item in evidence["preliminary_independent_checks"]))

        run_dir = Path(summary["run_dir"])
        self.assertEqual({path.name for path in run_dir.iterdir()}, {
            "01_neon_task_prompt.txt", "01_neon_task_stdout.txt", "01_neon_task_stderr.txt",
            "01_neon_task_parsed.json", "02_sol_prompt.txt", "02_sol_stdout.jsonl",
            "02_sol_stderr.txt", "02_sol_parsed.json", "03_neon_review_prompt.txt",
            "03_neon_review_stdout.txt", "03_neon_review_stderr.txt", "03_neon_review_parsed.json",
            "decision_record.json", "run_summary.json",
        })
        self.assertEqual((run_dir / "02_sol_stdout.jsonl").read_bytes(), codex_stdout().encode("utf-8"))
        self.assertEqual((run_dir / "02_sol_prompt.txt").read_text(encoding="utf-8"), sol_prompt)
        self.assertEqual((run_dir / "02_sol_stderr.txt").read_text(encoding="utf-8"), "执行诊断")
        self.assertIn("答案正确", (run_dir / "decision_record.json").read_text(encoding="utf-8"))
        self.assertEqual(read_json(run_dir / "01_neon_task_parsed.json")["payload"], TASK)
        self.assertEqual(read_json(run_dir / "02_sol_parsed.json")["payload"], ANSWER)
        self.assertEqual(read_json(run_dir / "03_neon_review_parsed.json")["payload"], REVIEW)
        self.assertEqual(read_json(run_dir / "run_summary.json"), summary)
        self.assertEqual(summary["steps"]["sol"]["usage"]["output_tokens"], 7)
        self.assertEqual(summary["steps"]["neon_review"]["conversation_id"], CONVERSATION_ID)

    def test_pass_is_overridden_on_nonzero_sol_exit(self):
        summary, decision, mocked = self._run(sol=completed(codex_stdout(), code=9))
        self._assert_override(summary, decision, "sol.exit_code")
        self.assertEqual(mocked.call_count, 3)
        evidence = json.loads(mocked.call_args_list[2].args[0][-1].split("\n\n", 1)[1])
        self.assertEqual(evidence["sol_exit_code"], 9)
        self.assertEqual(evidence["sol_thread_id"], THREAD_ID)

    def test_pass_is_overridden_on_resume_mismatch(self):
        summary, decision, _ = self._run(review=completed(agy_stdout(REVIEW, conversation="new-session")))
        self._assert_override(summary, decision, "neon_review.same_conversation")

    def test_pass_is_overridden_when_neon_review_fails(self):
        for output, code, failed_check in (
            (agy_stdout(REVIEW, status="failed"), 0, "neon_review.status"),
            (agy_stdout(REVIEW), 2, "neon_review.exit_code"),
            (agy_stdout({**REVIEW, "issues": None}), 0, "neon_review.issues"),
        ):
            with self.subTest(failed_check=failed_check):
                summary, decision, _ = self._run(review=completed(output, code=code))
                self._assert_override(summary, decision, failed_check)

    def test_pass_is_overridden_when_a_declared_rule_fails(self):
        scenarios = [
            (codex_stdout({**ANSWER, "sum": 4}), "sol.exact.sum"),
            (codex_stdout({**ANSWER, "sum": 5.0}), "sol.exact.sum"),
            (codex_stdout({"task_id": TASK["task_id"]}), "sol.required.sum"),
            (codex_stdout({**ANSWER, "task_id": "other"}), "sol.task_id_matches"),
            (codex_stdout(message=json_text(ANSWER)), "sol.must_contain.0"),
            (codex_stdout(completed=False), "sol.turn_completed"),
            (codex_stdout() + "malformed\n", "sol.jsonl"),
        ]
        for stdout, failed_check in scenarios:
            with self.subTest(failed_check=failed_check):
                summary, decision, _ = self._run(sol=completed(stdout))
                self._assert_override(summary, decision, failed_check)

    def test_pass_is_overridden_on_timeout_with_partial_stdout(self):
        partial = codex_stdout(completed=False).encode("utf-8")
        summary, decision, mocked = self._run(
            sol=subprocess.TimeoutExpired(cmd="mock", timeout=180, output=partial, stderr=b"timeout"),
        )
        self._assert_override(summary, decision, "sol.timeout")
        self.assertEqual(mocked.call_count, 3)
        sol = summary["steps"]["sol"]
        self.assertTrue(sol["timed_out"])
        self.assertIsNone(sol["exit_code"])
        self.assertEqual(sol["thread_id"], THREAD_ID)
        self.assertEqual(Path(sol["paths"]["stdout"]).read_bytes(), partial)

    def test_pass_is_overridden_on_invalid_utf8_even_when_stdout_is_valid(self):
        result = completed(codex_stdout())
        result.stderr = b"diagnostics\xff"
        summary, decision, _ = self._run(sol=result)
        self._assert_override(summary, decision, "sol.utf8")
        paths = summary["steps"]["sol"]["paths"]
        self.assertIn("\ufffd", Path(paths["stderr"]).read_text(encoding="utf-8"))
        self.assertEqual(base64.b64decode(read_json(paths["parsed"])["invalid_utf8_base64"]["stderr"]),
                         result.stderr)

    def test_pass_is_overridden_on_four_recorded_calls(self):
        task = Stage(ProcessResult(exit_code=0), parse_agy_json(agy_stdout(TASK)), TASK)
        sol = Stage(ProcessResult(exit_code=0), parse_codex_jsonl(codex_stdout()), ANSWER)
        review = Stage(ProcessResult(exit_code=0), parse_agy_json(agy_stdout(REVIEW)), REVIEW)
        self.assertEqual(verify_loop(task, sol, review, call_count=3)["final_decision"], "PASS")
        decision = verify_loop(task, sol, review, call_count=4)
        self._assert_override({"exit_code": 1}, decision, "loop.call_limit")

    def test_invalid_first_step_stops_with_audit_files(self):
        task = completed(agy_stdout({**TASK, "exact_checks": {}, "must_contain": []}))
        summary, decision, mocked = self._run(task=task)
        self.assertEqual(mocked.call_count, 1)
        self.assertIsNone(decision["model_decision"])
        self.assertEqual(summary["final_decision"], "FAILED")
        self.assertEqual(len(list(Path(summary["run_dir"]).iterdir())), 14)
        for step in ("sol", "neon_review"):
            self.assertFalse(summary["steps"][step]["attempted"])
            self.assertTrue(summary["steps"][step]["skipped_reason"])

    def test_valid_nonpass_model_decisions_are_preserved_without_retry(self):
        for model_decision in ("RETRY", "NEED_HUMAN"):
            with self.subTest(model_decision=model_decision):
                summary, decision, mocked = self._run(
                    review=completed(agy_stdout({**REVIEW, "decision": model_decision})),
                )
                self.assertEqual(mocked.call_count, 3)
                self.assertTrue(decision["program_passed"])
                self.assertEqual(decision["final_decision"], model_decision)
                self.assertIsNone(decision["override_reason"])
                self.assertNotEqual(summary["exit_code"], 0)

    def test_cli_uses_overrides_and_returns_program_exit_code(self):
        for code, expected in ((0, 0), (7, 1)):
            with self.subTest(codex_exit=code):
                responses = [completed(agy_stdout(TASK)), completed(codex_stdout(), code=code),
                             completed(agy_stdout(REVIEW))]
                with patch("agentbridge.process.subprocess.run", side_effect=responses) as mocked:
                    console = io.StringIO()
                    with redirect_stdout(console):
                        exit_code = run_bridge_loop.main([
                            "--timeout-s", "47", "--sol-effort", "high", "--output-dir",
                            str(self.config.output_dir), "--agy-path", "mock-agy.exe",
                            "--codex-path", "mock-codex.cmd",
                        ])
                self.assertEqual(exit_code, expected)
                self.assertEqual(json.loads(console.getvalue())["model_decision"], "PASS")
                self.assertEqual(mocked.call_args_list[0].args[0][0], str(WORKDIR / "mock-agy.exe"))
                self.assertIn("47s", mocked.call_args_list[0].args[0])
                self.assertEqual(mocked.call_args_list[1].args[0][0], str(WORKDIR / "mock-codex.cmd"))
                self.assertIn("model_reasoning_effort=high", mocked.call_args_list[1].args[0])
