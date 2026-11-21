"""Workspace paths, content snapshots, and durable run files."""

import codecs
import difflib
import hashlib
import io
import json
import os
import stat
from contextlib import contextmanager
from fnmatch import fnmatchcase
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep

from .bridge import WORKDIR


class IntegrityError(ValueError):
    """An input or audit path violated the run's filesystem boundary."""


HASH_CHUNK = 65536
SENSITIVE_NAMES = {".ssh", ".codex", ".aws", ".git"}
SYSTEM_NAMES = {"windows", "program files", "program files (x86)", "programdata",
                "system volume information", "$recycle.bin"}
FILE_POLICY = {
    "ignore_globs": [".git/**", "__pycache__/**", "*.pyc", ".pytest_cache/**",
                     "*.tmp", ".venv/**", "node_modules/**"],
    "stream_hash_globs": [],
    "max_text_diff_bytes": 262144,
    "max_patch_bytes": 524288,
    "max_full_hash_bytes": 16777216,
    "max_audit_stream_bytes": 262144,
    "max_context_chars": 16000,
}


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


def authorized_path(path: Path, *, allow_external: bool = False) -> Path:
    original = WORKDIR / path
    if os.name == "nt" and (not original.is_absolute() or original.drive.startswith("\\")
                            or any(":" in part or Path(part).is_reserved()
                                   or (part != ".." and part.endswith((".", " ")))
                                   for part in original.parts[1:])):
        raise IntegrityError(f"ambiguous Windows path: {original}")
    path = Path(os.path.abspath(original))
    if not allow_external:
        workspace_path(path)
    homes = {Path.home(), *(Path(value) for key in ("USERPROFILE", "HOME")
                           if (value := os.environ.get(key)))}
    system = [Path(value) for key in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
              if (value := os.environ.get(key))]
    profile_root = (len(path.parts) in (2, 3)
                    and path.parts[1].casefold() in {"users", "documents and settings"})
    parts = tuple(part.casefold() for part in path.parts[1:])
    if (not parts or path == WORKDIR or path in homes or profile_root or parts[0] in SYSTEM_NAMES
            or SENSITIVE_NAMES.intersection(parts) or any(path.is_relative_to(root) for root in system)):
        raise IntegrityError(f"protected system or workspace path: {path}")
    # Check traversal components before normalization, including a link followed by '..'.
    if ".." in original.parts:
        cursor = Path(original.anchor)
        for part in original.parts[1:]:
            cursor = cursor.parent if part == ".." else cursor / part
            if cursor != Path(cursor.anchor):
                safe_kind(cursor)
    return path


def checked_authorized_path(path: Path, kind: str | None = None, *,
                            allow_external: bool = False, missing_ok: bool = False) -> Path:
    path = authorized_path(path, allow_external=allow_external)
    for parent in reversed(path.parents):
        if parent == Path(path.anchor):
            continue
        actual = safe_kind(parent)
        if actual != "directory" and not (missing_ok and actual is None):
            raise IntegrityError(f"expected a directory: {parent}")
    actual = safe_kind(path)
    if (actual is None and not missing_ok) or (actual is not None and kind and actual != kind):
        raise IntegrityError(f"expected an ordinary {kind or 'path'}: {path}")
    if path.resolve() != path and os.name != "nt":
        raise IntegrityError(f"path must use its canonical name: {path}")
    if os.name == "nt":
        resolved = path.resolve()
        if tuple(part.casefold() for part in resolved.parts) != tuple(part.casefold() for part in path.parts):
            raise IntegrityError(f"path must use its canonical name: {path}")
        path = resolved
    return path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _glob(name, patterns):
    if os.name == "nt":
        name, patterns = name.casefold(), (pattern.casefold() for pattern in patterns)
    parts = name.split("/")
    for pattern in patterns:
        if "/" not in pattern and fnmatchcase(parts[-1], pattern):
            return True
        for index in range(len(parts)):
            suffix = "/".join(parts[index:])
            if fnmatchcase(suffix, pattern) or (
                    pattern.endswith("/**") and fnmatchcase(suffix, pattern[:-3])):
                return True
    return False


def walk_entries(root, *, skip=(), ignore=(), protected=()):
    """Yield safe files and ignored directory boundaries, using slash-separated names."""
    if safe_kind(root) != "directory":
        raise IntegrityError(f"expected a directory: {root}")
    protected = {name.casefold() if os.name == "nt" else name for name in protected}
    pending = [(root, False)]
    while pending:
        directory, parent_ignored = pending.pop()
        for entry in directory.iterdir():
            name = entry.relative_to(root).as_posix()
            kind = safe_kind(entry)
            if kind is None:
                raise IntegrityError(f"path disappeared during scan: {entry}")
            if name in skip:
                continue
            key = name.casefold() if os.name == "nt" else name
            ignored = key not in protected and (parent_ignored or _glob(name, ignore))
            if kind == "directory":
                contains_protected = any(item.startswith(key + "/") for item in protected)
                pending.append((entry, ignored))
                if ignored and not parent_ignored and not contains_protected:
                    yield name, entry, True
            elif not parent_ignored or not ignored:
                yield name, entry, ignored


def stream_manifest(directory, *, previous=None, skip=()):
    """Index files by metadata and digest, reusing sealed metadata matches."""
    previous = previous or {}
    entries = {}
    for name, path, _ in walk_entries(directory, skip=skip):
        entries[name] = stream_entry(path, previous.get(name))
    return entries


def stream_entry(path, previous=None):
    if safe_kind(path) != "file":
        raise IntegrityError(f"expected a file: {path}")
    stat_result = path.stat()
    entry = {"size_bytes": stat_result.st_size, "mtime_ns": stat_result.st_mtime_ns,
             "integrity_mode": "stream_hash_only"}
    if previous and all(previous.get(key) == value for key, value in entry.items()):
        return previous
    return {**entry, "sha256": file_sha256(path)}


def digests(entries):
    return {name: entry if isinstance(entry, str) else entry["sha256"] for name, entry in entries.items()}


def _small_file(path, entry, spool):
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")()
    start = spool.tell() if spool is not None else 0
    size = 0
    with path.open("rb") as source:
        for content in iter(lambda: source.read(HASH_CHUNK), b""):
            size += len(content)
            if size > entry["size_bytes"]:
                raise IntegrityError(f"file changed during snapshot: {path}")
            digest.update(content)
            if decoder is not None:
                try:
                    if b"\0" in content:
                        raise UnicodeError("binary file")
                    decoder.decode(content, final=(size == entry["size_bytes"]))
                except UnicodeError:
                    decoder = None
                else:
                    if spool is not None:
                        spool.write(content)
    if size != entry["size_bytes"]:
        raise IntegrityError(f"file changed during snapshot: {path}")
    if decoder is None and spool is not None:
        spool.seek(start)
        spool.truncate()
    mode = "content_and_hash" if decoder is not None else "stream_hash_only"
    return {**entry, "integrity_mode": mode, "sha256": digest.hexdigest()}, [start, size]


def tracked_snapshot(root, policy, *, spool=None, protected=()):
    """Keep a manifest in memory and optionally spool text for a durable pre-edit diff."""
    entries = {}
    locations = {}
    single = safe_kind(root) == "file"
    protected_keys = {name.casefold() if os.name == "nt" else name for name in protected}
    paths = ((root.name, root, False),) if single else walk_entries(
        root, ignore=policy["ignore_globs"], protected=protected)
    for name, path, ignored in paths:
        if ignored:
            entries[name] = {"integrity_mode": "ignored"}
            continue
        info = path.stat()
        entry = {"size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns}
        key = name.casefold() if os.name == "nt" else name
        if (info.st_size > policy["max_full_hash_bytes"] and not single and key not in protected_keys
                and not _glob(name, policy["stream_hash_globs"])):
            entries[name] = {**entry, "integrity_mode": "metadata_only"}
            continue
        if info.st_size <= policy["max_text_diff_bytes"]:
            entries[name], location = _small_file(path, entry, spool)
            if spool is not None and entries[name]["integrity_mode"] == "content_and_hash":
                locations[name] = location
        else:
            entries[name] = stream_entry(path)
    return entries, locations


def tracked_identity(entry):
    if entry is None or entry["integrity_mode"] == "ignored":
        return None
    return entry.get("sha256") or (entry["size_bytes"], entry["mtime_ns"])


def tracked_equal(before, after):
    return all(tracked_identity(before.get(name)) == tracked_identity(after.get(name))
               for name in before.keys() | after.keys())


def tracked_patch(before, after, project, spool, locations, limit):
    changes = {"added": [], "modified": [], "deleted": [], "details": {}}
    output = bytearray()
    truncated = False
    marker = b"\n[patch truncated: max_patch_bytes exceeded]\n"

    def append(lines):
        nonlocal truncated
        for line in lines:
            chunk = line.encode("utf-8")
            remaining = limit - len(output)
            output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
                break

    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        old_id, new_id = tracked_identity(old), tracked_identity(new)
        if old_id == new_id:
            continue
        action = "added" if old_id is None else "deleted" if new_id is None else "modified"
        changes[action].append(name)
        changes["details"][name] = {"before": old, "after": new}
        if truncated:
            continue
        modes = {item["integrity_mode"] for item in (old, new)
                 if item is not None and item["integrity_mode"] != "ignored"}
        if modes == {"content_and_hash"} and (old_id is None or name in locations):
            old_lines = []
            if old_id is not None:
                offset, size = locations[name]
                spool.seek(offset)
                old_lines = _lines(spool.read(size))
            new_lines = []
            if new_id is not None:
                with (project / name).open("rb") as source:
                    content = source.read(new["size_bytes"] + 1)
                if hashlib.sha256(content).hexdigest() != new_id:
                    raise IntegrityError(f"project file changed during diff: {name}")
                new_lines = _lines(content)
            left = f"a/{name}" if old_id is not None else "/dev/null"
            right = f"b/{name}" if new_id is not None else "/dev/null"
            diff = difflib.unified_diff(old_lines, new_lines, fromfile=left, tofile=right)
            first = next(diff, None)
            if first is None:
                append([f"--- {left}\n", f"+++ {right}\n"])
            else:
                append([first])
                append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                       for line in diff)
        else:
            label = ("metadata_only: content integrity was not verified (未做内容完整性校验)"
                     if "metadata_only" in modes else "stream_hash_only: SHA-256 / size changed")
            append([f"{action} {name} [{label}] {json_text(changes['details'][name])}\n"])
    if truncated:
        output = output[:max(0, limit - len(marker))] + marker[:limit]
    return changes, output.decode("utf-8", errors="ignore"), truncated


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
    def __init__(self, directory: Path, *, allow_external=False):
        self.allow_external = allow_external
        self.directory = (checked_authorized_path(directory, "directory", allow_external=True)
                          if allow_external else checked_path(directory, "directory"))

    def path(self, name: str) -> Path:
        if Path(name).name != name:
            raise IntegrityError(f"invalid audit file name: {name!r}")
        if self.allow_external:
            return checked_authorized_path(self.directory / name, "file", allow_external=True,
                                           missing_ok=True)
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
