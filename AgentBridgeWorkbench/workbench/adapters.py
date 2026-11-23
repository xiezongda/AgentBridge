"""Invoke CLI and HTTP providers and translate their output to one response shape."""

from __future__ import annotations

from collections.abc import Mapping
from http.client import HTTPException
import inspect
import json
import os
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


_AGY_ARGV_LIMIT = 15_000
_TRUNCATION_MARKER = "[context truncated]"


def _windows_command_length(argv):
    command_line = subprocess.list2cmdline(argv)
    return len(command_line.encode("utf-16-le", errors="surrogatepass")) // 2


def build_agy_command(
    executable, model_id, prompt, timeout_s, *, agy_project=None, extra_dirs=None,
    agy_mode="plan", enable_plugins_and_skills=False, effort=None, agy_agent=None,
):
    """Bound the full Windows command line, including quoting and escaping."""
    args = [
        os.fspath(executable), "--model", model_id, "--output-format", "json",
        "--mode", agy_mode,
    ]
    if not enable_plugins_and_skills:
        args.append("--disable-slash-commands")
    if agy_project:
        args.extend(("--project", os.fspath(agy_project)))
    for directory in extra_dirs or ():
        args.extend(("--add-dir", os.fspath(directory)))
    if effort:
        args.extend(("--effort", effort))
    if agy_agent:
        args.extend(("--agent", agy_agent))
    args.extend(("--print-timeout", f"{timeout_s}s", "--print"))
    if len(prompt) < _AGY_ARGV_LIMIT and _windows_command_length([*args, prompt]) < _AGY_ARGV_LIMIT:
        return [*args, prompt]

    suffix = f"\n{_TRUNCATION_MARKER}"
    if _windows_command_length([*args, suffix]) >= _AGY_ARGV_LIMIT:
        raise ValueError("AGY executable and model arguments leave no room for the prompt")

    low, high = 0, min(max(0, len(prompt) - 1), _AGY_ARGV_LIMIT - 1)
    while low < high:
        middle = (low + high + 1) // 2
        bounded_prompt = prompt[:middle] + suffix
        if _windows_command_length([*args, bounded_prompt]) < _AGY_ARGV_LIMIT:
            low = middle
        else:
            high = middle - 1
    return [*args, prompt[:low] + suffix]


def build_codex_command(
    executable, model_id, effort, sandbox_mode="read-only", workdir=".", *,
    extra_dirs=None, codex_profile=None, codex_trust=False, extra_cli_config=None,
):
    """Supply the prompt through stdin and keep each chat call ephemeral."""
    args = [
        os.fspath(executable), "exec", "-m", model_id, "-c",
        f"model_reasoning_effort={effort}", "-s", sandbox_mode, "-C",
        os.fspath(workdir), "--ephemeral", "--skip-git-repo-check", "--json",
    ]
    for directory in extra_dirs or ():
        args.extend(("--add-dir", os.fspath(directory)))
    if codex_profile:
        args.extend(("-p", codex_profile))
    if codex_trust:
        args.append("--trust")
    for entry in extra_cli_config or ():
        args.extend(("-c", entry))
    return [*args, "-"]


def _as_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8-sig", errors="replace")
    return (value or "").lstrip("\ufeff")


def _error_text(error, fallback):
    if isinstance(error, Mapping):
        error = error.get("message") or error.get("detail")
    return error if isinstance(error, str) and error else fallback


def _first_field(objects, names, default=None):
    for obj in objects:
        for name in names:
            value = obj.get(name)
            if value is not None:
                return value
    return default


def parse_agy_stdout(stdout):
    """Read the JSON print result, preserving provider usage details."""
    try:
        payload = json.loads(_as_text(stdout))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid AGY JSON output") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid AGY JSON result")
    if (
        payload.get("error") or payload.get("success") is False
        or payload.get("is_error") is True or payload.get("status") in ("error", "failed", "failure")
    ):
        raise ValueError(_error_text(payload.get("error") or payload, "AGY reported an error"))

    body = payload
    for key in ("result", "response", "output", "data"):
        if isinstance(payload.get(key), dict):
            body = payload[key]
            break
    if body.get("error"):
        raise ValueError(_error_text(body["error"], "AGY reported an error"))

    output_text = next(
        (
            obj[key]
            for obj in (body, payload)
            for key in ("output_text", "response", "output", "text", "result", "content", "message")
            if isinstance(obj.get(key), str)
        ),
        None,
    )
    if output_text is None:
        raise ValueError("AGY JSON output did not contain response text")
    return {
        "output_text": output_text,
        "session_id": _first_field((body, payload), ("session_id", "sessionId", "conversation_id")),
        "usage": _first_field((body, payload), ("usage", "token_usage", "tokenUsage", "stats"), {}),
    }


def parse_codex_stdout(stdout):
    """Read Codex JSONL events and return the completed assistant response."""
    session_id = None
    usage = {}
    last_text = None
    final_text = None

    for line_number, line in enumerate(_as_text(stdout).split("\n"), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid Codex JSON event on line {line_number}") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError(f"Invalid Codex event on line {line_number}")

        event_type = event["type"]
        if event_type in {"turn.failed", "error"}:
            raise ValueError(_error_text(event.get("error") or event, "Codex reported an error"))
        if event_type == "thread.started":
            session_id = event.get("thread_id") or event.get("session_id")
        if event_type == "turn.completed":
            usage = event.get("usage", {})
        if event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise ValueError(f"Invalid Codex item on line {line_number}")
            if item["type"] == "error":
                raise ValueError(_error_text(item, "Codex reported an error"))
            if item["type"] == "agent_message":
                if not isinstance(item.get("text"), str):
                    raise ValueError(f"Invalid Codex agent message on line {line_number}")
                last_text = item["text"]
                if item.get("phase") == "final":
                    final_text = last_text

    output_text = final_text if final_text is not None else last_text
    if output_text is None:
        raise ValueError("Codex returned no completed agent message")
    return {"output_text": output_text, "session_id": session_id, "usage": usage}


class _NoProviderRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Send credentials only to the endpoint configured on the model card.
        return None


def _invoke_http(card, prompt, timeout_s):
    provider = card["provider"]
    is_openai = provider == "openai_compat"
    endpoint = "/chat/completions" if is_openai else "/api/chat"
    payload = {
        "model": card["model_id"],
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    options = {}
    for source, target in (
        ("temperature", "temperature"), ("max_tokens", "max_tokens" if is_openai else "num_predict"),
    ):
        if card.get(source) is not None:
            options[target] = card[source]
    if is_openai:
        payload.update(options)
    elif options:
        payload["options"] = options

    try:
        key_env = card.get("api_key_env")
        key = (os.environ.get(key_env) if key_env else None) or card.get("api_key")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = Request(
            card["base_url"].rstrip("/") + endpoint,
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with build_opener(_NoProviderRedirects).open(request, timeout=timeout_s) as response:
            body = json.load(response)
    except HTTPError as exc:
        exc.close()
        raise RuntimeError(f"{provider} returned HTTP {exc.code}") from None
    except TimeoutError:
        raise RuntimeError(f"{provider} timed out after {timeout_s:g} seconds") from None
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise RuntimeError(f"{provider} timed out after {timeout_s:g} seconds") from None
        raise RuntimeError(f"Could not connect to {provider}") from None
    except (HTTPException, OSError, TypeError, ValueError):
        raise RuntimeError(f"{provider} request failed or returned invalid JSON") from None

    try:
        if not isinstance(body, dict) or body.get("error"):
            raise ValueError
        if is_openai:
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage")
        else:
            content = body["message"]["content"]
            usage = {
                field: body[field] for field in ("prompt_eval_count", "eval_count")
                if type(body.get(field)) is int
            }
        if not isinstance(content, str):
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError(f"{provider} did not return a chat message") from None
    return {"output_text": content, "session_id": None, "usage": usage if isinstance(usage, dict) else {}}


class AdapterRegistry:
    """Invoke providers, with injectable CLI runners for offline use."""

    def __init__(self, process_runner=None):
        self._process_runner = process_runner

    def _run_process(self, argv, *, input_text, timeout_s, workdir, node_id):
        options = {
            "input": input_text,
            "timeout": timeout_s,
            "cwd": workdir,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": False,
        }
        if self._process_runner is None or self._process_runner is subprocess.run:
            if input_text is None:
                options["stdin"] = subprocess.DEVNULL
            return subprocess.run(argv, **options)

        # Test runners can declare just the process inputs they need, using seconds.
        runner = self._process_runner
        try:
            parameters = list(inspect.signature(runner).parameters.values())
        except (TypeError, ValueError):
            return runner(argv, **options)
        options["node_id"] = node_id
        commands = dict.fromkeys(("argv", "args", "command", "cmd"), argv)
        available = {
            **options,
            **commands,
            "input_text": input_text,
            "stdin_text": input_text,
            "stdin": input_text,
            "timeout_s": timeout_s,
            "workdir": workdir,
        }
        positional = []
        if parameters and parameters[0].kind in (
            inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional.append(argv)
            parameters = parameters[1:]
        keywords = {}
        accepts_keywords = False
        for parameter in parameters:
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                accepts_keywords = True
            elif parameter.kind == inspect.Parameter.VAR_POSITIONAL:
                if not positional and not any(p.name in commands for p in parameters):
                    positional.append(argv)
            elif parameter.name in available:
                if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                    positional.append(available[parameter.name])
                else:
                    keywords[parameter.name] = available[parameter.name]
        if accepts_keywords:
            if not positional and not commands.keys() & keywords.keys():
                keywords["command"] = argv
            keywords = {**options, **keywords}
        return runner(*positional, **keywords)

    def invoke(self, card, prompt, *, effort, timeout_s, sandbox_mode, workdir=None, node_id=None):
        provider = card["provider"]
        if provider in ("openai_compat", "ollama"):
            return _invoke_http(card, prompt, timeout_s)

        workdir = workdir if workdir is not None else card.get("workdir")
        if workdir and os.fspath(workdir) != ".":
            workdir = os.path.abspath(workdir)
        if provider == "agy_cli":
            argv = build_agy_command(
                card["executable"], card["model_id"], prompt, timeout_s,
                agy_project=card.get("agy_project"), extra_dirs=card.get("extra_dirs"),
                agy_mode=card.get("agy_mode") or "plan",
                enable_plugins_and_skills=card.get("enable_plugins_and_skills", False),
                effort=effort, agy_agent=card.get("agy_agent"),
            )
            input_text = None
            parser = parse_agy_stdout
        elif provider == "codex_cli":
            argv = build_codex_command(
                card["executable"], card["model_id"], effort, sandbox_mode, workdir or ".",
                extra_dirs=card.get("extra_dirs"), codex_profile=card.get("codex_profile"),
                codex_trust=card.get("codex_trust", False), extra_cli_config=card.get("extra_cli_config"),
            )
            input_text = prompt
            parser = parse_codex_stdout
        else:
            raise ValueError(f"Unsupported model provider: {provider}")

        try:
            result = self._run_process(
                argv, input_text=input_text, timeout_s=timeout_s, workdir=workdir or None, node_id=node_id,
            )
        except subprocess.TimeoutExpired as exc:
            raise subprocess.TimeoutExpired([argv[0]], exc.timeout) from exc

        if isinstance(result, tuple):
            if len(result) != 3:
                raise ValueError("Process runner tuple must contain returncode, stdout, and stderr")
            returncode, stdout, stderr = result
        elif isinstance(result, Mapping):
            returncode = result.get("returncode", 0)
            stdout = result.get("stdout")
            stderr = result.get("stderr")
        else:
            returncode = result.returncode
            stdout = result.stdout
            stderr = result.stderr
        if returncode:
            detail = _as_text(stderr).strip() or _as_text(stdout).strip()
            detail = f": {detail}" if detail else ""
            raise RuntimeError(f"{provider} exited with code {returncode}{detail}")
        return parser(stdout)

    def test_connection(self, card):
        started = time.monotonic()
        try:
            result = self.invoke(
                card,
                "Reply with the single word OK.",
                effort=card.get("effort", card.get("default_effort", "high")),
                timeout_s=card.get("timeout_s", card.get("default_timeout_s", 300)),
                sandbox_mode=card.get("sandbox_mode", card.get("default_sandbox_mode", "read-only")),
            )
            ok = True
            message = result["output_text"].strip() or "Connection successful"
            error = None
        except (KeyError, OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as exc:
            ok = False
            message = "Connection failed"
            error = str(exc)

        return {
            "ok": ok,
            "provider": card.get("provider"),
            "model_id": card.get("model_id"),
            "latency_ms": round((time.monotonic() - started) * 1_000),
            "message": message,
            "error": error,
        }
