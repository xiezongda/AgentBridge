"""Parse CLI output without executing any model-supplied content."""

import json
import math
import mmap
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
_BYTE_START = re.compile(rb"[\[{]")
_BYTE_TOKENS = re.compile(rb'\\.|["{}\[\]]', re.DOTALL)
_BYTE_DECLARED = re.compile(rb'\{\s*"')


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


def parse_agy_stream(stream, record_limit):
    """Search disk-backed stdout for a complete JSON envelope, without copying log noise."""
    size = stream.seek(0, 2)
    stream.seek(0)
    if size <= record_limit:
        return parse_agy_json(stream.read(record_limit).decode("utf-8"))
    with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        first = re.search(rb"\S", data)
        position, found = 0, None
        while opening := _BYTE_START.search(data, position):
            start = opening.start()
            boundary = min(size, start + record_limit)
            end = _frame_end(data, start, boundary)
            if end is None and boundary < size:
                raise ValueError(f"Neon protocol record exceeds {record_limit} bytes")
            try:
                if not end:
                    raise ValueError("incomplete JSON frame")
                frame = data[start:end].decode("utf-8")
                value, _ = _JSON_BOUNDARIES.raw_decode(frame)
            except ValueError:
                if opening[0] == b"{" and (start == first.start() or _BYTE_DECLARED.match(data, start, boundary)):
                    raise ValueError("malformed JSON object or array") from None
                position = start + 1
                continue
            position = end
            if isinstance(value, dict):
                if found is not None:
                    raise ValueError("multiple JSON objects found")
                found = _load_json(frame)
        if found is None:
            raise ValueError("no JSON object found")
        return found


def _frame_end(data, start, boundary):
    stack = []
    quoted = False
    for match in _BYTE_TOKENS.finditer(data, start, boundary):
        token = match[0]
        if token == b'"':
            quoted = not quoted
        elif not quoted and token in (b"{", b"["):
            stack.append(b"}" if token == b"{" else b"]")
            if len(stack) > 1000:
                raise ValueError("Neon protocol record is nested too deeply")
        elif not quoted and token in (b"}", b"]"):
            if not stack or stack.pop() != token:
                return 0
            if not stack:
                return match.end()
    return None


def parse_model_object(response: object) -> dict:
    if isinstance(response, dict):
        return response
    if isinstance(response, str):
        return extract_json_object(response)
    raise ValueError("expected a JSON object in the response")


def parse_codex_jsonl(stdout) -> dict:
    thread_id = None
    thread_events = 0
    message = None
    completed_turns = 0
    usage = None
    errors = []
    failure_events = []
    legacy = isinstance(stdout, str)
    lines = stdout.split("\n") if legacy else stdout
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = _load_json(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("expected a JSON event with a string type")
        except (ValueError, RecursionError) as exc:
            if legacy or len(errors) < 20:
                errors.append(f"line {line_number}: {exc}")
            continue

        event_type = event["type"]
        if event_type == "thread.started":
            thread_events += 1
            thread_id = event.get("thread_id")
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                message = item.get("text")
        elif event_type == "turn.completed":
            completed_turns += 1
            usage = event.get("usage")
        elif event_type == "turn.failed" and (legacy or not failure_events):
            failure_events.append(event_type)

    return {
        "thread_id": thread_id if thread_events == 1 else None,
        "thread_event_count": thread_events,
        "agent_message": message,
        "completed_turns": completed_turns,
        "usage": usage,
        "parse_errors": errors,
        "failure_events": failure_events,
    }
