"""Check collected evidence and combine it with the model's recommendation."""

from dataclasses import asdict, dataclass, field

from .process import ProcessResult

MAX_CALLS = 3
AGY_SUCCESS = {"success", "succeeded", "ok", "completed"}
MODEL_DECISIONS = {"PASS", "RETRY", "NEED_HUMAN"}


@dataclass
class Stage:
    process: ProcessResult | None = None
    envelope: dict | None = None
    payload: dict | None = None
    envelope_error: str | None = None
    payload_error: str | None = None


@dataclass
class Check:
    name: str
    passed: bool
    reason: str | None


def _check(name: str, passed: bool, failure_reason: str) -> Check:
    return Check(name, passed, None if passed else failure_reason)


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _text_list(value: object) -> bool:
    return isinstance(value, list) and all(_nonempty_text(item) for item in value)


def _json_equal(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _json_equal(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _json_equal(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def _check_process(name: str, process: ProcessResult | None) -> list[Check]:
    if process is None:
        return [_check(f"{name}.attempted", False, "stage was not invoked")]
    return [
        _check(f"{name}.attempted", True, ""),
        _check(f"{name}.launch", process.launch_error is None, process.launch_error or ""),
        _check(f"{name}.timeout", not process.timed_out, "subprocess timed out"),
        _check(f"{name}.utf8", process.utf8_ok, "invalid UTF-8 in subprocess output"),
        _check(f"{name}.exit_code", process.exit_code == 0, f"exit code: {process.exit_code}"),
    ]


def _check_neon(name: str, stage: Stage) -> list[Check]:
    envelope = stage.envelope or {}
    status = envelope.get("status")
    conversation = envelope.get("conversation_id")
    return _check_process(name, stage.process) + [
        _check(f"{name}.json", stage.envelope is not None, stage.envelope_error or "missing JSON"),
        _check(
            f"{name}.status",
            isinstance(status, str) and status.casefold() in AGY_SUCCESS,
            f"unsuccessful agy status: {status!r}",
        ),
        _check(
            f"{name}.conversation_id",
            _nonempty_text(conversation) and "\0" not in conversation,
            "missing or invalid conversation_id",
        ),
        _check(
            f"{name}.payload", stage.payload is not None, stage.payload_error or "missing payload"
        ),
    ]


def verify_task(stage: Stage) -> list[Check]:
    task = stage.payload or {}
    required = task.get("required_output_keys")
    exact = task.get("exact_checks", {})
    contains = task.get("must_contain", [])
    exact_valid = isinstance(exact, dict) and all(_nonempty_text(key) for key in exact)
    return _check_neon("neon_task", stage) + [
        _check("task.task_id", _nonempty_text(task.get("task_id")), "missing task_id"),
        _check("task.instruction", _nonempty_text(task.get("instruction")), "missing instruction"),
        _check(
            "task.required_output_keys",
            _text_list(required) and bool(required) and len(set(required)) == len(required),
            "required_output_keys must be a nonempty list of distinct strings",
        ),
        _check("task.exact_checks", exact_valid, "exact_checks must be an object with nonempty keys"),
        _check("task.must_contain", _text_list(contains), "must_contain must be a list of strings"),
        _check(
            "task.content_rule",
            (exact_valid and bool(exact.keys() - {"task_id"}))
            or (_text_list(contains) and bool(contains)),
            "task must define at least one check beyond task_id and key presence",
        ),
    ]


def _verify_sol(stage: Stage, task: dict | None) -> list[Check]:
    envelope = stage.envelope or {}
    message = envelope.get("agent_message")
    checks = _check_process("sol", stage.process) + [
        _check(
            "sol.jsonl",
            stage.envelope is not None and envelope.get("parse_errors") == [],
            "; ".join(envelope.get("parse_errors", [])) or "missing JSONL events",
        ),
        _check("sol.thread_id", _nonempty_text(envelope.get("thread_id")), "missing unique thread_id"),
        _check("sol.agent_message", _nonempty_text(message), "missing final agent_message"),
        _check(
            "sol.turn_completed",
            envelope.get("completed_turns") == 1 and envelope.get("failure_events") == [],
            "expected one turn.completed and no turn.failed event",
        ),
        _check("sol.payload", stage.payload is not None, stage.payload_error or "missing result"),
    ]
    if task is None or stage.payload is None:
        return checks + [_check("sol.task_rules", False, "validated task or result unavailable")]

    result = stage.payload
    checks.append(
        _check("sol.task_id_matches", result.get("task_id") == task["task_id"], "task_id mismatch")
    )
    for key in task["required_output_keys"]:
        checks.append(_check(f"sol.required.{key}", key in result, f"missing required key {key!r}"))
    for key, expected in task.get("exact_checks", {}).items():
        checks.append(
            _check(
                f"sol.exact.{key}",
                key in result and _json_equal(result[key], expected),
                f"key {key!r} does not match the declared JSON value",
            )
        )
    for index, token in enumerate(task.get("must_contain", [])):
        checks.append(
            _check(
                f"sol.must_contain.{index}",
                isinstance(message, str) and token in message,
                f"agent message is missing {token!r}",
            )
        )
    return checks


def verify_execution(task: Stage, sol: Stage) -> list[Check]:
    task_checks = verify_task(task)
    valid_task = task.payload if all(check.passed for check in task_checks) else None
    return task_checks + _verify_sol(sol, valid_task)


def verify_loop(task: Stage, sol: Stage, review: Stage, *, call_count: int) -> dict:
    checks = [
        _check("loop.call_limit", 0 <= call_count <= MAX_CALLS, f"call count: {call_count}"),
        _check("loop.three_calls", call_count == MAX_CALLS, f"expected 3 calls, saw {call_count}"),
    ]
    checks.extend(verify_execution(task, sol))
    checks.extend(_check_neon("neon_review", review))
    first_id = (task.envelope or {}).get("conversation_id")
    review_id = (review.envelope or {}).get("conversation_id")
    review_payload = review.payload or {}
    model_decision = review_payload.get("decision")
    valid_decision = isinstance(model_decision, str) and model_decision in MODEL_DECISIONS
    checks.extend([
        _check(
            "neon_review.same_conversation",
            _nonempty_text(first_id) and review_id == first_id,
            "review conversation_id differs from the task conversation_id or is missing",
        ),
        _check("neon_review.decision", valid_decision, "expected PASS, RETRY or NEED_HUMAN"),
        _check("neon_review.reason", _nonempty_text(review_payload.get("reason")), "missing reason"),
        _check(
            "neon_review.checked_items",
            _text_list(review_payload.get("checked_items")),
            "checked_items must be a list of strings",
        ),
        _check(
            "neon_review.issues",
            _text_list(review_payload.get("issues")),
            "issues must be a list of strings",
        ),
    ])
    failed = [check.name for check in checks if not check.passed]
    return {
        "call_count": call_count,
        "model_decision": model_decision,
        "program_passed": not failed,
        "independent_checks": [asdict(check) for check in checks],
        "final_decision": model_decision if not failed else "FAILED",
        "override_reason": f"independent checks failed: {', '.join(failed)}" if failed else None,
    }
