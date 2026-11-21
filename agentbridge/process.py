"""Capture subprocess streams as bytes and decode them explicitly."""

import base64
import codecs
import io
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryFile
from time import monotonic

from .protocol import parse_agy_stream

CHUNK_BYTES = 65536
MAX_PROTOCOL_RECORD_BYTES = 16 * 1024 * 1024


def audit_excerpt(text, limit):
    raw = text.encode("utf-8")
    return _bounded_bytes(io.BytesIO(raw), limit)


def _bounded_bytes(stream, limit):
    size = stream.seek(0, 2)
    stream.seek(0)
    if size <= limit:
        return stream.read(limit).decode("utf-8", errors="ignore")
    head = stream.read(limit // 2).decode("utf-8", errors="ignore")
    stream.seek(size - (limit - limit // 2))
    tail = stream.read(limit - limit // 2).decode("utf-8", errors="ignore")
    return {"excerpt": head + tail, "head_chars": len(head),
            "truncated": True, "original_bytes": size}


@dataclass
class ProcessResult:
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    elapsed_seconds: float = 0.0
    timed_out: bool = False
    launch_error: str | None = None
    invalid_utf8_base64: dict[str, str] = field(default_factory=dict)
    _streams: dict = field(default_factory=dict, repr=False)

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

    def audit(self, limit):
        streams = {name: (_bounded_bytes(self._streams[name], limit) if name in self._streams
                          else audit_excerpt(getattr(self, name), limit))
                   for name in ("stdout", "stderr")}
        metadata = self.audit_metadata()
        metadata["invalid_utf8_base64"] = {
            name: audit_excerpt(value, limit) for name, value in self.invalid_utf8_base64.items()
        }
        return {**streams, "process": metadata}

    def neon_output(self):
        if "stdout" not in self._streams:
            return self.stdout
        return parse_agy_stream(self._streams["stdout"], MAX_PROTOCOL_RECORD_BYTES)

    def lines(self, name, *, protocol=False):
        if name not in self._streams:
            yield from getattr(self, name).splitlines()
            return
        stream = self._streams[name]
        stream.seek(0)
        limit = MAX_PROTOCOL_RECORD_BYTES if protocol else CHUNK_BYTES
        while raw := stream.readline(limit + 1):
            if len(raw) > limit:
                if protocol:
                    raise ValueError("Codex protocol record exceeds 16 MiB")
                while raw and not raw.endswith(b"\n"):
                    raw = stream.readline(limit + 1)
                yield ""
            else:
                yield raw.decode("utf-8", errors="replace").removesuffix("\n").removesuffix("\r")

    def chunks(self, name, *, errors="replace"):
        if name not in self._streams:
            yield getattr(self, name)
            return
        stream = self._streams[name]
        stream.seek(0)
        decoder = codecs.getincrementaldecoder("utf-8")(errors=errors)
        for raw in iter(lambda: stream.read(CHUNK_BYTES), b""):
            yield decoder.decode(raw)
        yield decoder.decode(b"", final=True)

    def contains(self, terms, *, channels=("stdout", "stderr")):
        remaining = set(terms)
        if not remaining:
            return True
        keep = max(map(len, remaining)) - 1
        tail = ""
        for index, name in enumerate(channels):
            for chunk in self._joined_chunks(name, index + 1 < len(channels)):
                text = tail + chunk
                remaining.difference_update(term for term in tuple(remaining) if term in text)
                if not remaining:
                    return True
                tail = text[-keep:] if keep else ""
        return False

    def _joined_chunks(self, name, separator):
        yield from self.chunks(name)
        if separator:
            yield "\n"


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


@contextmanager
def captured_process(runner, argv, *, cwd, timeout_s, stdin=None, env=None):
    """Use disk-backed capture for the framework while preserving injected runner APIs."""
    if runner is not run_process:
        yield runner(argv, cwd=cwd, timeout_s=timeout_s, stdin=stdin, env=env)
        return
    started = monotonic()
    with TemporaryFile() as stdout, TemporaryFile() as stderr:
        result = ProcessResult(_streams={"stdout": stdout, "stderr": stderr})
        try:
            completed = subprocess.run(argv, cwd=cwd, timeout=timeout_s, shell=False,
                                       stdout=stdout, stderr=stderr, input=stdin,
                                       stdin=subprocess.DEVNULL if stdin is None else None, env=env)
            result.exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            result.timed_out = True
        except (OSError, ValueError) as exc:
            result.launch_error = str(exc)
        result.elapsed_seconds = round(monotonic() - started, 6)
        for name, stream in result._streams.items():
            stream.seek(0)
            decoder = codecs.getincrementaldecoder("utf-8")()
            try:
                for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                    decoder.decode(chunk)
                decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                result.invalid_utf8_base64[name] = base64.b64encode(exc.object[exc.start:exc.end]).decode("ascii")
        yield result
