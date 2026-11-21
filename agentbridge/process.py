"""Capture subprocess streams as bytes and decode them explicitly."""

import base64
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic


@dataclass
class ProcessResult:
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    elapsed_seconds: float = 0.0
    timed_out: bool = False
    launch_error: str | None = None
    invalid_utf8_base64: dict[str, str] = field(default_factory=dict)

    @property
    def utf8_ok(self) -> bool:
        return not self.invalid_utf8_base64

    def audit_metadata(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "elapsed_seconds": self.elapsed_seconds,
            "timed_out": self.timed_out,
            "launch_error": self.launch_error,
            "utf8_ok": self.utf8_ok,
            "invalid_utf8_base64": self.invalid_utf8_base64,
        }


def _decode(data: bytes | None, name: str, invalid: dict[str, str]) -> str:
    if data is None:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        invalid[name] = base64.b64encode(data).decode("ascii")
        return data.decode("utf-8", errors="replace")


def run_process(
    argv: list[str], *, cwd: Path, timeout_s: int | float, stdin: bytes | None = None,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    started = monotonic()
    result = ProcessResult()
    stdout = stderr = None
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout_s,
            shell=False,
            capture_output=True,
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            **({"env": env} if env is not None else {}),
        )
        result.exit_code = completed.returncode
        stdout, stderr = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        result.timed_out = True
        stdout, stderr = exc.stdout, exc.stderr
    except (OSError, ValueError) as exc:
        result.launch_error = str(exc)

    result.elapsed_seconds = round(monotonic() - started, 6)
    result.stdout = _decode(stdout, "stdout", result.invalid_utf8_base64)
    result.stderr = _decode(stderr, "stderr", result.invalid_utf8_base64)
    return result
