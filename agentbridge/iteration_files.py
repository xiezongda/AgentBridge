"""Workspace paths, content snapshots, and durable run files."""

import difflib
import hashlib
import io
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep

from .bridge import WORKDIR


class IntegrityError(ValueError):
    """An input or audit path violated the run's filesystem boundary."""


def workspace_path(path: Path) -> Path:
    path = Path(os.path.abspath(WORKDIR / path))
    if path == WORKDIR or not path.is_relative_to(WORKDIR):
        raise IntegrityError(f"path must be below {WORKDIR}: {path}")
    if os.name == "nt" and any(
        ":" in part or part.endswith((".", " ")) for part in path.relative_to(WORKDIR).parts
    ):
        raise IntegrityError(f"ambiguous Windows path: {path}")
    return path


def overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def safe_kind(path: Path) -> str | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    reparse = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if reparse or stat.S_ISLNK(info.st_mode):
        raise IntegrityError(f"linked path is forbidden: {path}")
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
        return "file"
    raise IntegrityError(f"expected an ordinary, unlinked file or directory: {path}")


def checked_path(path: Path, kind: str, *, missing_ok: bool = False) -> Path:
    path = workspace_path(path)
    for parent in reversed(path.parents):
        if parent == WORKDIR or not parent.is_relative_to(WORKDIR):
            continue
        actual = safe_kind(parent)
        if actual != "directory" and not (missing_ok and actual is None):
            raise IntegrityError(f"expected a directory: {parent}")
    actual = safe_kind(path)
    if actual != kind and not (missing_ok and actual is None):
        raise IntegrityError(f"expected a {kind}: {path}")
    if path.resolve() != path:
        raise IntegrityError(f"path must use its canonical name: {path}")
    return path


def snapshot(directory: Path) -> dict[str, bytes]:
    checked_path(directory, "directory")
    files = {}

    def visit(folder: Path, ignored: bool = False) -> None:
        for entry in folder.iterdir():
            kind = safe_kind(entry)
            if kind == "directory":
                visit(entry, ignored or entry.name.casefold() == "__pycache__")
            elif kind == "file" and not ignored and entry.suffix.casefold() != ".pyc":
                files[entry.relative_to(directory).as_posix()] = entry.read_bytes()
            elif kind is None:
                raise IntegrityError(f"path disappeared during snapshot: {entry}")

    visit(directory)
    return files


def manifest(files: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}


def _lines(content: bytes) -> list[str]:
    if b"\0" in content:
        raise UnicodeError("binary file")
    return io.StringIO(content.decode("utf-8"), newline="\n").readlines()


def changes_and_patch(before: dict[str, bytes], after: dict[str, bytes]) -> tuple[dict, str]:
    changes = {
        "added": sorted(after.keys() - before.keys()),
        "modified": sorted(name for name in before.keys() & after.keys() if before[name] != after[name]),
        "deleted": sorted(before.keys() - after.keys()),
    }
    patch = []
    for name in sorted(sum(changes.values(), [])):
        old = f"a/{name}" if name in before else "/dev/null"
        new = f"b/{name}" if name in after else "/dev/null"
        try:
            old_lines = _lines(before.get(name, b""))
            new_lines = _lines(after.get(name, b""))
        except UnicodeError:
            patch.append(f"Binary files {old} and {new} differ\n")
            continue
        diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=old, tofile=new))
        if not diff:
            diff = [f"--- {old}\n", f"+++ {new}\n"]
        for line in diff:
            patch.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return changes, "".join(patch)


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


class RunFiles:
    def __init__(self, directory: Path):
        self.directory = checked_path(directory, "directory")

    def path(self, name: str) -> Path:
        if Path(name).name != name:
            raise IntegrityError(f"invalid audit file name: {name!r}")
        return checked_path(self.directory / name, "file", missing_ok=True)

    def write(self, name: str, text: str) -> None:
        path = self.path(name)
        with NamedTemporaryFile(dir=self.directory, prefix=".write-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
                for attempt in range(6):
                    try:
                        os.replace(temporary, path)
                        break
                    except PermissionError:
                        if os.name != "nt" or attempt == 5:
                            raise
                        sleep(0.01)
            finally:
                stream.close()
                temporary.unlink(missing_ok=True)

    def read(self, name: str) -> dict:
        return json.loads(self.path(name).read_text(encoding="utf-8"))

    def record(self, state: dict, event: str, details: dict) -> None:
        self.write("state.json", json_text(state) + "\n")
        with self.path("events.jsonl").open("ab") as stream:
            stream.write((json_text({
                "event": event, "state": state["current_state"], "round": state["current_round"],
                "elapsed_seconds": state["elapsed_seconds"], **details,
            }) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())

    @contextmanager
    def lock(self):
        # Keep the file in place. The OS releases the byte/inode lock on process exit.
        with self.path("run.lock").open("a+b", buffering=0) as stream:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError(f"run is already locked or inaccessible: {self.directory}") from exc
            yield
