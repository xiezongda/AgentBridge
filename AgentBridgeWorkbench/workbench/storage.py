"""Thread-safe SQLite persistence for the workbench."""

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .graph import validate_workflow
from .models import (
    DEFAULT_TEMPLATES,
    default_model_cards,
    node_config,
    normalize_model_card,
    normalize_template,
    normalize_workflow,
    redact_model_card,
    require_text,
)

_PRIVATE_BUNDLE_FIELDS = frozenset({"workdir", "extra_dirs", "executable", "api_key", "api_key_env"})


class ConflictError(ValueError):
    """A requested change conflicts with the current database state."""


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _now():
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _portable_workflow(value):
    if isinstance(value, dict):
        return {
            key: _portable_workflow(item) for key, item in value.items()
            if key not in _PRIVATE_BUNDLE_FIELDS
        }
    if isinstance(value, list):
        return [_portable_workflow(item) for item in value]
    return value


def _validate_portable_workflow(workflow):
    result = validate_workflow(workflow, require_executable=False)
    if not result["valid"]:
        raise ValueError("\n".join(result["errors"]))


def _redact_turn(record):
    snapshot = record.get("model_cards_snapshot")
    if isinstance(snapshot, dict):
        record["model_cards_snapshot"] = {
            card_id: redact_model_card(card) for card_id, card in snapshot.items()
        }
    return record


class WorkbenchStore:
    def __init__(self, db_path):
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._conn.execute("PRAGMA busy_timeout=5000")
                self._initialize()
        except BaseException:
            self.close()
            raise

    def _initialize(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS model_cards (
                id TEXT PRIMARY KEY,
                record TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS templates (
                id TEXT PRIMARY KEY,
                record TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                workflow TEXT NOT NULL,
                workflow_version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                record TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS turns_by_conversation ON turns(conversation_id);
        """)
        with self._conn:
            seeded = self._conn.execute(
                "INSERT OR IGNORE INTO settings VALUES ('default_catalog_seeded')"
            ).rowcount
            if seeded:
                for table, defaults in (
                    ("model_cards", default_model_cards()),
                    ("templates", DEFAULT_TEMPLATES),
                ):
                    records = defaults.values() if isinstance(defaults, dict) else defaults
                    self._conn.executemany(
                        f"INSERT OR IGNORE INTO {table} (id, record) VALUES (?, ?)",
                        ((record["id"], _encode(record)) for record in records),
                    )

    def close(self):
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def _list_records(self, table):
        with self._lock:
            rows = self._conn.execute(f"SELECT record FROM {table} ORDER BY rowid")
            return [json.loads(row["record"]) for row in rows]

    def _get_record(self, table, record_id):
        with self._lock:
            row = self._conn.execute(
                f"SELECT record FROM {table} WHERE id = ?", (record_id,)
            ).fetchone()
            return json.loads(row["record"]) if row else None

    def _create_record(self, table, data, normalize):
        record = normalize({"id": uuid4().hex, **data})
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    f"INSERT INTO {table} (id, record) VALUES (?, ?)",
                    (record["id"], _encode(record)),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"ID already exists: {record['id']}") from exc
        return record

    def _update_record(self, table, record_id, data, merge):
        with self._lock, self._conn:
            current = self._get_record(table, record_id)
            if current is None:
                raise KeyError(record_id)
            record = merge(current, {**data, "id": record_id})
            self._conn.execute(
                f"UPDATE {table} SET record = ? WHERE id = ?",
                (_encode(record), record_id),
            )
            return record

    def _delete_record(self, table, record_id):
        with self._lock, self._conn:
            result = self._conn.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
            if not result.rowcount:
                raise KeyError(record_id)
        return True

    def list_model_cards(self):
        return [normalize_model_card(card) for card in self._list_records("model_cards")]

    def get_model_card(self, card_id):
        card = self._get_record("model_cards", card_id)
        return normalize_model_card(card) if card is not None else None

    def create_model_card(self, card):
        return self._create_record("model_cards", card, normalize_model_card)

    def update_model_card(self, card_id, card):
        return self._update_record(
            "model_cards", card_id, card,
            lambda current, changes: normalize_model_card(current, changes=changes),
        )

    def delete_model_card(self, card_id):
        return self._delete_record("model_cards", card_id)

    def list_templates(self):
        return self._list_records("templates")

    def get_template(self, template_id):
        return self._get_record("templates", template_id)

    def create_template(self, template):
        return self._create_record("templates", template, normalize_template)

    def update_template(self, template_id, template):
        return self._update_record(
            "templates", template_id, template,
            lambda current, changes: normalize_template({**current, **changes}),
        )

    def delete_template(self, template_id):
        return self._delete_record("templates", template_id)

    def export_workflow(self, data):
        workflow = data.get("workflow")
        name = data.get("name")
        description = data.get("description", "")
        with self._lock:
            if workflow is None and data.get("conversation_id") is not None:
                conversation_id = require_text(data["conversation_id"], "conversation_id")
                conversation = self.get_conversation(conversation_id)
                if conversation is None:
                    raise KeyError(conversation_id)
                workflow = conversation["workflow"]
                if name is None:
                    name = conversation["title"]
            name = require_text("Untitled workflow" if name is None else name, "name")
            if not isinstance(description, str):
                raise ValueError("description must be a string")
            _validate_portable_workflow(workflow)
            workflow = _portable_workflow(workflow)
            card_ids = dict.fromkeys(
                node_config(node)["model_card_id"]
                for node in workflow["nodes"] if node["type"] == "model"
            )
            catalog = {card["id"]: card for card in self.list_model_cards()}
            slots = []
            for card_id in filter(None, card_ids):
                card = catalog.get(card_id, {})
                slots.append({
                    "model_card_id": card_id,
                    **{field: card[field] for field in ("name", "provider", "model_id") if field in card},
                })
        return {
            "format_version": 2, "name": name, "description": description,
            "workflow": workflow, "model_slots": slots,
        }

    def import_workflow(self, data):
        bundle = data.get("bundle")
        if not isinstance(bundle, dict) or bundle.get("format_version") != 2:
            raise ValueError("bundle must be a version 2 workflow bundle")
        mapping = data.get("card_mapping", {})
        if not isinstance(mapping, dict) or any(
            not isinstance(old, str) or not old.strip()
            or not isinstance(local, str) or not local.strip()
            for old, local in mapping.items()
        ):
            raise ValueError("card_mapping must map source card IDs to local card IDs")
        save = data.get("save_as_template", False)
        if type(save) is not bool:
            raise ValueError("save_as_template must be a boolean")
        workflow = _portable_workflow(bundle.get("workflow"))
        _validate_portable_workflow(workflow)
        workflow = normalize_workflow(workflow)

        with self._lock:
            card_ids = {card["id"] for card in self.list_model_cards()}
            unmapped = []
            for node in workflow["nodes"]:
                if node["type"] != "model":
                    continue
                config = node["config"]
                card_id = mapping.get(config["model_card_id"], config["model_card_id"])
                config["model_card_id"] = card_id
                if "model_card_id" in node:
                    node["model_card_id"] = card_id
                if card_id not in card_ids:
                    unmapped.append({"node_id": node["id"], "model_card_id": card_id})
            result = {"workflow": workflow, "unmapped_nodes": unmapped}
            if save:
                result["template"] = self.create_template({
                    "name": bundle.get("name", "Imported workflow"),
                    "description": bundle.get("description", ""),
                    "workflow": workflow,
                })
        return result

    @staticmethod
    def _conversation(row):
        if row is None:
            return None
        return {**dict(row), "workflow": json.loads(row["workflow"])}

    def list_conversations(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC, rowid DESC"
            )
            return [self._conversation(row) for row in rows]

    def get_conversation(self, conversation_id, *, include_turns=False):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            conversation = self._conversation(row)
            if conversation is not None and include_turns:
                conversation["turns"] = self.list_turns(conversation_id)
            return conversation

    def create_conversation(self, title="New conversation", workflow=None, *, template_id=None):
        if isinstance(title, dict):
            data = title
            title = data.get("title", "New conversation")
            workflow = data.get("workflow", workflow)
            template_id = data.get("template_id", template_id)
        self._validate_title(title)
        with self._lock, self._conn:
            if workflow is None:
                template = self.get_template(template_id or "direct_sol")
                if template is None and template_id is not None:
                    raise KeyError(template_id)
                if template is None:
                    defaults = (
                        DEFAULT_TEMPLATES.values()
                        if isinstance(DEFAULT_TEMPLATES, dict) else DEFAULT_TEMPLATES
                    )
                    template = next(record for record in defaults if record["id"] == "direct_sol")
                workflow = template["workflow"]
            conversation_id = uuid4().hex
            now = _now()
            self._conn.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, 1, ?, ?)",
                (conversation_id, title.strip(), _encode(normalize_workflow(workflow)), now, now),
            )
            return self.get_conversation(conversation_id)

    @staticmethod
    def _validate_title(title):
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Conversation title must be a non-empty string.")

    def update_conversation(self, conversation_id, changes):
        with self._lock, self._conn:
            current = self.get_conversation(conversation_id)
            if current is None:
                raise KeyError(conversation_id)
            title = changes.get("title", current["title"])
            self._validate_title(title)
            version = changes.get("workflow_version")
            if version is not None:
                if type(version) is not int or version < 1:
                    raise ValueError("workflow_version must be a positive integer.")
                if version != current["workflow_version"]:
                    raise ConflictError("The workflow has changed. Reload the conversation.")
            workflow = current["workflow"]
            next_version = current["workflow_version"]
            if "workflow" in changes:
                workflow = normalize_workflow(changes["workflow"])
                next_version += 1
            self._conn.execute(
                "UPDATE conversations SET title = ?, workflow = ?, workflow_version = ?, "
                "updated_at = ? WHERE id = ?",
                (title.strip(), _encode(workflow), next_version, _now(), conversation_id),
            )
            return self.get_conversation(conversation_id)

    def update_conversation_workflow(self, conversation_id, workflow, *, workflow_version=None):
        return self.update_conversation(
            conversation_id, {"workflow": workflow, "workflow_version": workflow_version}
        )

    def delete_conversation(self, conversation_id):
        return self._delete_record("conversations", conversation_id)

    def create_turn(self, conversation_id, turn=None, **fields):
        record = _redact_turn({
            **(turn or {}),
            **fields,
            "conversation_id": conversation_id,
        })
        record.setdefault("id", uuid4().hex)
        record.setdefault("created_at", _now())
        with self._lock, self._conn:
            self._append_turn(record)
        return record

    def _append_turn(self, record):
        try:
            self._conn.execute(
                "INSERT INTO turns (id, conversation_id, record) VALUES (?, ?, ?)",
                (record["id"], record["conversation_id"], _encode(record)),
            )
        except sqlite3.IntegrityError as exc:
            if self.get_conversation(record["conversation_id"]) is None:
                raise KeyError(record["conversation_id"]) from exc
            raise ConflictError(f"Turn ID already exists: {record['id']}") from exc

    def list_turns(self, conversation_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT record FROM turns WHERE conversation_id = ? ORDER BY rowid",
                (conversation_id,),
            )
            return [_redact_turn(json.loads(row["record"])) for row in rows]

    def get_turn(self, conversation_id, turn_id=None):
        with self._lock:
            if turn_id is None:
                row = self._conn.execute(
                    "SELECT record FROM turns WHERE id = ?", (conversation_id,)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT record FROM turns WHERE conversation_id = ? AND id = ?",
                    (conversation_id, turn_id),
                ).fetchone()
            return _redact_turn(json.loads(row["record"])) if row else None

    def restore_turn_workflow(self, conv_id, turn_id):
        with self._lock, self._conn:
            turn = self.get_turn(conv_id, turn_id)
            if turn is None:
                raise KeyError(turn_id)
            self._conn.execute(
                "UPDATE conversations SET workflow = ?, workflow_version = workflow_version + 1, "
                "updated_at = ? WHERE id = ?",
                (_encode(turn["workflow_snapshot"]), _now(), conv_id),
            )
            return self.get_conversation(conv_id)

    def capture_run_context(self, conversation_id):
        with self._lock:
            conversation = self.get_conversation(conversation_id, include_turns=True)
            if conversation is None:
                raise KeyError(conversation_id)
            return conversation, {card["id"]: card for card in self.list_model_cards()}
