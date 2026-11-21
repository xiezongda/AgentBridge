"""Parse CLI output without executing any model-supplied content."""

import json
import math
import re


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON number {value!r}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _reject_constant(value)
    return number


_STRICT_JSON = json.JSONDecoder(
    object_pairs_hook=_unique_keys, parse_constant=_reject_constant, parse_float=_finite_float
)
# The first decoder identifies object boundaries even when a value or key is invalid.
_JSON_BOUNDARIES = json.JSONDecoder()
_JSON_START = re.compile(r"[\[{]")


def _load_json(text: str) -> object:
    text.encode("utf-8")
    value = _STRICT_JSON.decode(text)
    # Escaped lone surrogates can pass JSON decoding but cannot be written as UTF-8.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return value


def extract_json_object(text: str) -> dict:
    """Accept a single JSON object, optionally surrounded by fences or prose."""
    position = 0
    found = None
    while match := _JSON_START.search(text, position):
        start = match.start()
        try:
            value, position = _JSON_BOUNDARIES.raw_decode(text, start)
        except json.JSONDecodeError:
            # An opening brace at the start of the payload declares a JSON value.
            # Reject a malformed value rather than extracting one of its children.
            if text[start] == "{" and (
                not text[:start].strip() or re.match(r'\{\s*"', text[start:])
            ):
                raise ValueError("malformed JSON object or array") from None
            position = start + 1
            continue
        if isinstance(value, dict):
            if found is not None:
                raise ValueError("multiple JSON objects found")
            found = _load_json(text[start:position])
    if found is None:
        raise ValueError("no JSON object found")
    return found


def parse_agy_json(stdout: str) -> dict:
    return extract_json_object(stdout)


def parse_model_object(response: object) -> dict:
    if isinstance(response, dict):
        return response
    if isinstance(response, str):
        return extract_json_object(response)
    raise ValueError("expected a JSON object in the response")


def parse_codex_jsonl(stdout: str) -> dict:
    thread_ids = []
    message = None
    completed_turns = 0
    usage = None
    errors = []
    failure_events = []
    for line_number, line in enumerate(stdout.split("\n"), 1):
        if not line.strip():
            continue
        try:
            event = _load_json(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("expected a JSON event with a string type")
        except (ValueError, RecursionError) as exc:
            errors.append(f"line {line_number}: {exc}")
            continue

        event_type = event["type"]
        if event_type == "thread.started":
            thread_ids.append(event.get("thread_id"))
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                message = item.get("text")
        elif event_type == "turn.completed":
            completed_turns += 1
            usage = event.get("usage")
        elif event_type == "turn.failed":
            failure_events.append(event_type)

    return {
        "thread_id": thread_ids[0] if len(thread_ids) == 1 else None,
        "thread_event_count": len(thread_ids),
        "agent_message": message,
        "completed_turns": completed_turns,
        "usage": usage,
        "parse_errors": errors,
        "failure_events": failure_events,
    }
