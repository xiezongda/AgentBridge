"""Translate CLI output into review evidence and independently check test results."""

import json
import re
import sys

from .bridge import NEON_MODEL, SOL_MODEL
from .iteration_files import json_text
from .process import ProcessResult
from .protocol import parse_agy_json, parse_codex_jsonl, parse_model_object
from .verify import AGY_SUCCESS, MODEL_DECISIONS

SESSION_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", re.ASCII)


class NeonUnavailable(ValueError):
    """Neon reported that the invocation did not succeed."""

# Resolve unittest and the Python startup environment before making project code
# importable. A fresh cache prefix also prevents discovery from loading old .pyc.
_UNITTEST_ENTRY = """\
import json
import sys
import unittest
tests, project, cache = sys.argv[1:]
proof = sys.stdin.buffer.read().decode("ascii")
sys.stdin.close()
write = sys.stdout.write
serialize = json.dumps
sys.pycache_prefix = cache
sys.path.append(project)
program = unittest.main(module=None, argv=["unittest", "discover", "-s", tests, "-v"], exit=False)
result = program.result
passed = result.wasSuccessful()
write("\\nAGENTBRIDGE_TEST_RESULT " + serialize([proof, result.testsRun, len(result.skipped), passed]) + "\\n")
raise SystemExit(5 if passed and not result.testsRun and not result.skipped else int(not passed))
"""


def neon_command(path, timeout: int, prompt: str, conversation: str | None) -> list[str]:
    argv = [str(path), "--model", NEON_MODEL, "--output-format", "json", "--mode", "plan",
            "--disable-slash-commands", "--print-timeout", f"{timeout}s"]
    if conversation:
        argv.extend(["--conversation", conversation])
    return argv + ["--print", prompt]


def sol_command(path, project, effort: str, thread: str | None) -> list[str]:
    model = ["-m", SOL_MODEL, "-c", f"model_reasoning_effort={effort}"]
    scope = ["-s", "workspace-write", "-C", str(project)]
    tail = ["--skip-git-repo-check", "--json"]
    argv = [str(path), "exec"]
    if thread:
        return argv + scope + ["resume"] + model + tail + [thread, "-"]
    return argv + model + scope + tail + ["-"]


def unittest_command(tests, project, cache) -> list[str]:
    return [sys.executable, "-I", "-B", "-X", "utf8", "-c", _UNITTEST_ENTRY,
            str(tests), str(project), str(cache)]


def session_id(value: object) -> str | None:
    if isinstance(value, str) and SESSION_ID.fullmatch(value):
        return value
    return None


def required_text(obj: dict, field: str) -> str:
    value = obj.get(field)
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"missing or invalid {field}")
    return value


def parse_neon(stdout: str | dict, conversation: str | None) -> tuple[dict, dict]:
    envelope = stdout if isinstance(stdout, dict) else parse_agy_json(stdout)
    status = envelope.get("status")
    if not isinstance(status, str):
        raise ValueError("missing Neon status")
    if status.casefold() not in AGY_SUCCESS:
        raise NeonUnavailable(f"unsuccessful Neon status: {status!r}")
    reported = session_id(envelope.get("conversation_id"))
    if not reported or (conversation and reported != conversation):
        raise ValueError("missing or mismatched Neon conversation_id")
    return envelope, parse_model_object(envelope.get("response"))


def parse_sol(stdout: str) -> tuple[dict, str | None]:
    envelope = parse_codex_jsonl(stdout)
    thread = session_id(envelope["thread_id"]) if not envelope["parse_errors"] else None
    return envelope, thread


def validate_sol(envelope: dict, thread: str | None, expected: str | None) -> None:
    if envelope["parse_errors"]:
        raise ValueError("invalid Sol JSONL")
    if not thread or (expected and thread != expected):
        raise ValueError("missing or mismatched Sol thread_id")
    if envelope["completed_turns"] != 1 or envelope["failure_events"]:
        raise ValueError("expected one completed Sol turn without failure events")
    required_text(envelope, "agent_message")


def validate_review(payload: dict) -> None:
    if required_text(payload, "decision") not in MODEL_DECISIONS:
        raise ValueError("expected Neon decision PASS, RETRY or NEED_HUMAN")
    required_text(payload, "reason")
    if "feedback" in payload:
        required_text(payload, "feedback")


def test_result(result: ProcessResult, min_tests: int, allow_skipped: bool,
                proof: str | None = None) -> dict:
    # Unittest writes its report to stderr by default. A trusted custom runner
    # may write the same report to stdout.
    reports = []
    skip_line = False
    receipts = []
    for stream in ("stdout", "stderr"):
        previous = ""
        awaiting_outcome = None
        for line in result.lines(stream):
            if awaiting_outcome is not None and line.strip():
                if len(reports) < 2:
                    reports.append((awaiting_outcome, line))
                awaiting_outcome = None
            match = re.fullmatch(r"Ran (\d{1,9}) tests? in [0-9.]+s", line)
            if match and previous == "-" * 70:
                awaiting_outcome = int(match[1])
            previous = line
            skip_line |= bool(re.search(r" \.\.\. skipped(?:\s|$)", line))
            if stream == "stdout" and line.startswith("AGENTBRIDGE_TEST_RESULT ") and len(receipts) < 2:
                receipts.append(line)
        if awaiting_outcome is not None and len(reports) < 2:
            reports.append((awaiting_outcome, ""))
    count, outcome = reports[0] if len(reports) == 1 else (0, "")
    skipped_match = re.search(r"\bskipped=(\d{1,9})\b", outcome)
    skipped = int(skipped_match[1]) if skipped_match else 0
    problem = process_problem(result)
    if result.utf8_ok and len(reports) == 1 and count == 0 and result.exit_code in {0, 5}:
        problem = f"expected one unittest report with at least {min_tests} executed tests"
    if not problem and (skipped or skip_line) and not allow_skipped:
        problem = "skipped tests are not allowed"
    if not problem and (len(reports) != 1 or count < min_tests):
        problem = f"expected one unittest report with at least {min_tests} executed tests"
    if not problem and not re.fullmatch(r"OK(?: \([^\r\n]*\))?", outcome):
        problem = "unittest did not report OK"
    if not problem and count - skipped < min_tests:
        problem = f"fewer than {min_tests} tests executed after excluding skips"
    if not problem and proof is not None:
        expected = "AGENTBRIDGE_TEST_RESULT " + json.dumps([proof, count, skipped, True])
        if receipts != [expected]:
            problem = "missing or inconsistent independent unittest completion receipt"
    return {"test_passed": problem is None, "tests_run": count, "skipped": skipped,
            "reason": problem, "process": result.audit_metadata()}


def process_problem(result: ProcessResult) -> str | None:
    if result.launch_error:
        return f"could not launch process: {result.launch_error}"
    if result.timed_out:
        return "process timed out"
    if not result.utf8_ok:
        return "invalid UTF-8 in process streams"
    if result.exit_code != 0:
        return f"process exited with code {result.exit_code}"
    return None


def excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit // 2] + "\n[... omitted; complete evidence saved in the run directory ...]\n" + text[-limit // 2:]


def bounded_json(value: object, limit: int) -> str:
    return excerpt(json_text(value), limit)
