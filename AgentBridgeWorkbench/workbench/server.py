"""Local HTTP API and built workbench frontend."""

import json
import logging
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .adapters import AdapterRegistry
from .executor import WorkflowExecutor
from .graph import validate_workflow
from .models import environment_info, redact_model_card
from .storage import ConflictError, WorkbenchStore

DEFAULT_STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"


class _WorkbenchServer(ThreadingHTTPServer):
    def __init__(self, address, *, store, static_dir, adapter_registry):
        self.store = store
        self.static_dir = static_dir
        self.adapter_registry = adapter_registry
        self.executor = WorkflowExecutor(store, adapter_registry)
        super().__init__(address, _Handler)

    def server_close(self):
        try:
            super().server_close()
        finally:
            self.store.close()


class _Handler(BaseHTTPRequestHandler):
    def _json(self, value, status=HTTPStatus.OK):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8")

    def _respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0:
            raise ValueError("Content-Length must be non-negative.")
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        return data

    @staticmethod
    def _found(record, record_id):
        if record is None:
            raise KeyError(record_id)
        return record

    def _validate(self, workflow, *, require_executable):
        if not isinstance(workflow, dict):
            raise ValueError("workflow must be an object.")
        if type(require_executable) is not bool:
            raise ValueError("require_executable must be a boolean.")
        return validate_workflow(
            workflow, self.server.store.list_model_cards(), require_executable=require_executable
        )

    def _editable_workflow(self, workflow):
        result = self._validate(workflow, require_executable=False)
        if not result["valid"]:
            raise ValueError("\n".join(result["errors"]))
        return workflow

    def _catalog(self, parts, *, kind, create, get, update, delete, list_records):
        def public_record(record):
            return redact_model_card(record) if kind == "models" else record

        method = self.command
        if not parts:
            if method in ("GET", "HEAD"):
                records = [public_record(record) for record in list_records()]
                return self._json({"templates": records} if kind == "templates" else records)
            if method == "POST":
                data = self._read_json()
                if kind == "templates":
                    self._editable_workflow(data.get("workflow"))
                return self._json(public_record(create(data)), HTTPStatus.CREATED)
        if len(parts) == 1:
            record_id = parts[0]
            if method in ("GET", "HEAD"):
                return self._json(public_record(self._found(get(record_id), record_id)))
            if method in ("PUT", "PATCH"):
                self._found(get(record_id), record_id)
                data = self._read_json()
                if kind == "templates" and "workflow" in data:
                    self._editable_workflow(data["workflow"])
                return self._json(public_record(update(record_id, data)))
            if method == "DELETE":
                delete(record_id)
                return self._json({"ok": True})
        if kind == "models" and len(parts) == 2 and parts[1] == "test" and method == "POST":
            self._read_json()
            card = self._found(get(parts[0]), parts[0])
            return self._json(self.server.adapter_registry.test_connection(card))
        return self._json({"error": "API route not found."}, HTTPStatus.NOT_FOUND)

    def _conversations(self, parts):
        method = self.command
        store = self.server.store
        if not parts:
            if method in ("GET", "HEAD"):
                return self._json(store.list_conversations())
            if method == "POST":
                data = self._read_json()
                if "workflow" in data:
                    self._editable_workflow(data["workflow"])
                if "template_id" in data and data["template_id"] is not None:
                    if not isinstance(data["template_id"], str) or not data["template_id"]:
                        raise ValueError("template_id must be a non-empty string.")
                conversation = store.create_conversation(data)
                return self._json({**conversation, "turns": []}, HTTPStatus.CREATED)
        if len(parts) == 1:
            conv_id = parts[0]
            if method in ("GET", "HEAD"):
                return self._json(self._found(
                    store.get_conversation(conv_id, include_turns=True), conv_id
                ))
            if method in ("PUT", "PATCH"):
                data = self._read_json()
                if "workflow" in data:
                    self._editable_workflow(data["workflow"])
                updated = store.update_conversation(conv_id, data)
                return self._json({**updated, "turns": store.list_turns(conv_id)})
            if method == "DELETE":
                store.delete_conversation(conv_id)
                return self._json({"ok": True})
        if len(parts) == 2:
            conv_id, action = parts
            if action == "workflow" and method == "PUT":
                data = self._read_json()
                workflow = self._editable_workflow(data.get("workflow", data))
                updated = store.update_conversation_workflow(
                    conv_id, workflow, workflow_version=data.get("workflow_version")
                )
                return self._json({**updated, "turns": store.list_turns(conv_id)})
            if action == "messages" and method == "POST":
                data = self._read_json()
                message = data.get("user_message", data.get("message", data.get("content")))
                turn = self.server.executor.run_turn(conv_id, message)
                return self._json(turn)
        if len(parts) == 3 and parts[1] == "restore-turn-workflow" and method == "POST":
            self._read_json()
            conv_id, _, turn_id = parts
            restored = store.restore_turn_workflow(conv_id, turn_id)
            return self._json({**restored, "turns": store.list_turns(conv_id)})
        return self._json({"error": "API route not found."}, HTTPStatus.NOT_FOUND)

    def _api(self, parts):
        method = self.command
        store = self.server.store
        if parts == ["health"] and method in ("GET", "HEAD"):
            return self._json({"ok": True, "status": "ok"})
        if parts == ["environment"] and method in ("GET", "HEAD"):
            return self._json(environment_info())
        if parts and parts[0] == "models":
            return self._catalog(
                parts[1:], kind="models", create=store.create_model_card, get=store.get_model_card,
                update=store.update_model_card, delete=store.delete_model_card,
                list_records=store.list_model_cards,
            )
        if parts and parts[0] == "templates":
            return self._catalog(
                parts[1:], kind="templates", create=store.create_template, get=store.get_template,
                update=store.update_template, delete=store.delete_template,
                list_records=store.list_templates,
            )
        if parts and parts[0] == "conversations":
            return self._conversations(parts[1:])
        if parts == ["workflows", "export"] and method == "POST":
            return self._json(store.export_workflow(self._read_json()))
        if parts == ["workflows", "import"] and method == "POST":
            return self._json(store.import_workflow(self._read_json()))
        if parts == ["workflows", "validate"] and method == "POST":
            data = self._read_json()
            result = self._validate(
                data.get("workflow", data), require_executable=data.get("require_executable", True)
            )
            return self._json(result)
        if parts == ["workflows", "preview-prompt"] and method == "POST":
            data = self._read_json()
            conv_id = data.get("conversation_id")
            if conv_id is not None and not isinstance(conv_id, str):
                raise ValueError("conversation_id must be a string.")
            node_id = data.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                raise ValueError("node_id must be a non-empty string.")
            workflow = data.get("workflow")
            if workflow is None and conv_id is not None:
                conversation = self._found(store.get_conversation(conv_id), conv_id)
                workflow = conversation["workflow"]
            result = self.server.executor.preview_prompt(
                conv_id, workflow, node_id, data.get("sample_user_message", "")
            )
            return self._json(result)
        return self._json({"error": "API route not found."}, HTTPStatus.NOT_FOUND)

    def _static(self, path):
        if self.command not in ("GET", "HEAD"):
            return self._json({"error": "Route not found."}, HTTPStatus.NOT_FOUND)
        parts = path.split("/")
        if "\\" in path or ":" in path or "\0" in path or any(
            part in (".", "..") for part in parts
        ):
            return self._json({"error": "Path not found."}, HTTPStatus.NOT_FOUND)
        root = self.server.static_dir
        target = (root / path.lstrip("/")).resolve()
        if not target.is_relative_to(root):
            return self._json({"error": "Path not found."}, HTTPStatus.NOT_FOUND)
        if not target.is_file() and not target.suffix:
            target = (root / "index.html").resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return self._json({"error": "File not found."}, HTTPStatus.NOT_FOUND)
        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        self._respond(HTTPStatus.OK, target.read_bytes(), content_type)

    def _dispatch(self):
        try:
            path = unquote(urlsplit(self.path).path)
            if path == "/api" or path.startswith("/api/"):
                parts = path.strip("/").split("/")[1:]
                self._api(parts)
            else:
                self._static(path)
        except ConflictError as exc:
            self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except KeyError as exc:
            self._json({"error": f"Resource not found: {exc.args[0]}"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except ConnectionError:
            return
        except Exception:
            logging.getLogger(__name__).exception("Workbench request failed")
            self._json({"error": "Internal server error."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch


def create_server(
    db_path, host="127.0.0.1", port=8765, *, static_dir=None, adapter_registry=None
):
    """Bind the HTTP server and initialize its store."""
    store = WorkbenchStore(db_path)
    try:
        static_root = Path(static_dir if static_dir is not None else DEFAULT_STATIC_DIR).resolve()
        registry = AdapterRegistry() if adapter_registry is None else adapter_registry
        return _WorkbenchServer(
            (host, port), store=store, static_dir=static_root, adapter_registry=registry,
        )
    except BaseException:
        store.close()
        raise
