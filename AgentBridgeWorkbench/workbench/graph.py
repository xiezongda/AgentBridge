"""DAG validation and prompt assembly shared by preview and execution."""

from collections.abc import Mapping

from .models import (
    DEFAULT_MODEL_CARDS,
    DEFAULT_SCHEDULE,
    EFFORTS,
    FAILURE_POLICIES,
    NODE_TYPES,
    SANDBOX_MODES,
    SCHEDULE_MINIMUMS,
    has_choice,
    is_integer_at_least,
    node_config,
    truncate_context,
    workspace_errors,
)


def _nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def _card_ids(model_cards):
    if isinstance(model_cards, Mapping):
        return set(model_cards)
    card_ids = set()
    for card in model_cards:
        card_id = card.get("id") if isinstance(card, Mapping) else card
        if _nonempty_text(card_id):
            card_ids.add(card_id)
    return card_ids


def _validate_schedule(workflow, errors):
    schedule = workflow.get("schedule", {})
    if not isinstance(schedule, dict):
        errors.append("schedule must be an object")
        return
    schedule = {**DEFAULT_SCHEDULE, **schedule}
    for field, minimum in SCHEDULE_MINIMUMS.items():
        if not is_integer_at_least(schedule[field], minimum):
            errors.append(f"schedule.{field} must be an integer >= {minimum}")
    if not has_choice(schedule["failure_policy"], FAILURE_POLICIES):
        errors.append("schedule.failure_policy must be stop_on_error or continue_on_error")


def _validate_workspace(workflow, errors):
    workspace = workflow.get("workspace")
    if workspace is None:
        return
    if not isinstance(workspace, dict):
        errors.append("workspace must be an object")
        return
    errors.extend(workspace_errors(workspace, "workspace"))
    override = workspace.get("sandbox_mode_override")
    if override is not None and not has_choice(override, SANDBOX_MODES):
        errors.append("workspace: invalid sandbox_mode_override")


def _validate_node_config(node, node_id, node_type, card_ids, require_executable, errors):
    try:
        config = node_config(node)
    except ValueError as error:
        errors.append(f"Node {node_id}: {error}")
        return
    if node_type == "context_input" and not isinstance(config["text"], str):
        errors.append(f"Node {node_id}: text must be a string")
    if node_type != "model":
        return
    if not isinstance(config["system_prompt"], str):
        errors.append(f"Node {node_id}: system_prompt must be a string")
    if type(config["include_history"]) is not bool:
        errors.append(f"Node {node_id}: include_history must be a boolean")
    if not isinstance(config["input_order"], list) or not all(map(_nonempty_text, config["input_order"])):
        errors.append(f"Node {node_id}: input_order must be an array of node IDs")
    for field, minimum in (("history_turns", 0), ("timeout_s_override", 1), ("max_retries_override", 0)):
        value = config.get(field)
        if value is None and field != "history_turns":
            continue
        if not is_integer_at_least(value, minimum):
            errors.append(f"Node {node_id}: {field} must be an integer >= {minimum}")
    for field, choices in (("effort_override", EFFORTS), ("sandbox_mode_override", SANDBOX_MODES)):
        value = config.get(field)
        if value is not None and not has_choice(value, choices):
            errors.append(f"Node {node_id}: invalid {field}")
    errors.extend(workspace_errors(config, f"Node {node_id}"))
    card_id = config["model_card_id"]
    if require_executable and (not _nonempty_text(card_id) or card_id not in card_ids):
        errors.append(f"Node {node_id}: unknown or missing model_card_id {card_id!r}")
    elif not isinstance(card_id, str):
        errors.append(f"Node {node_id}: model_card_id must be a string")


def _build_adjacency(edges, nodes, errors):
    outgoing = {node_id: [] for node_id in nodes}
    incoming = {node_id: [] for node_id in nodes}
    edge_ids = set()
    pairs = set()
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            errors.append(f"Edge {index + 1} must be an object")
            continue
        edge_id = edge.get("id", f"#{index + 1}")
        if "id" in edge:
            if not _nonempty_text(edge_id):
                errors.append(f"Edge {index + 1} id must be a non-empty string")
            elif edge_id in edge_ids:
                errors.append(f"Duplicate edge id: {edge_id}")
            else:
                edge_ids.add(edge_id)
        source, target = edge.get("source"), edge.get("target")
        if not _nonempty_text(source) or not _nonempty_text(target) or source not in nodes or target not in nodes:
            errors.append(f"Edge {edge_id} has an invalid source or target")
            continue
        if source == target:
            errors.append(f"Edge {edge_id} forms a self-loop at {source}")
            continue
        if (source, target) in pairs:
            errors.append(f"Duplicate edge from {source} to {target}")
            continue
        pairs.add((source, target))
        outgoing[source].append(target)
        incoming[target].append(source)
    return outgoing, incoming


def _topological_layers(outgoing, incoming):
    indegree = {node_id: len(sources) for node_id, sources in incoming.items()}
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    layers = []
    while ready:
        layers.append(ready)
        next_ready = []
        for node_id in ready:
            for target in outgoing[node_id]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    next_ready.append(target)
        ready = next_ready
    return layers


def _ancestors(incoming, final_node_id):
    active = set()
    pending = [final_node_id]
    while pending:
        current = pending.pop()
        if current not in active:
            active.add(current)
            pending.extend(incoming[current])
    return active


def validate_workflow(workflow, model_cards=None, *, require_executable=True):
    """Validate all nodes and edges, then return the schedule of the output's dependencies."""
    errors, warnings = [], []
    result = {"valid": False, "errors": errors, "warnings": warnings, "layers": [], "active_node_ids": []}
    if not isinstance(workflow, dict):
        errors.append("Workflow must be an object")
        return result
    _validate_schedule(workflow, errors)
    _validate_workspace(workflow, errors)
    nodes, edges = workflow.get("nodes"), workflow.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        errors.append("Workflow nodes and edges must be arrays")
        return result
    card_ids = _card_ids(DEFAULT_MODEL_CARDS if model_cards is None else model_cards)
    node_types = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict) or not _nonempty_text(node.get("id")):
            errors.append(f"Node {index + 1} id must be a non-empty string")
            continue
        node_id, node_type = node["id"], node.get("type")
        if node_id in node_types:
            errors.append(f"Duplicate node id: {node_id}")
            continue
        node_types[node_id] = node_type
        if not has_choice(node_type, NODE_TYPES):
            errors.append(f"Node {node_id} has an invalid type")
        else:
            _validate_node_config(node, node_id, node_type, card_ids, require_executable, errors)
    user_nodes = {node_id for node_id, node_type in node_types.items() if node_type == "user_input"}
    final_nodes = [node_id for node_id, node_type in node_types.items() if node_type == "final_output"]
    if not user_nodes:
        errors.append("Workflow must have at least one user_input node")
    if len(final_nodes) != 1:
        errors.append("Workflow must have exactly one final_output node")
    outgoing, incoming = _build_adjacency(edges, node_types, errors)
    layers = _topological_layers(outgoing, incoming)
    if sum(map(len, layers)) != len(node_types):
        errors.append("Workflow contains a cycle")
        layers = []
    if len(final_nodes) == 1:
        active = _ancestors(incoming, final_nodes[0])
        if not active.intersection(user_nodes):
            issue = "Workflow has no path from user_input to final_output"
            (errors if require_executable else warnings).append(issue)
        inactive = [node_id for node_id in node_types if node_id not in active]
        if inactive:
            warnings.append(f"Inactive nodes do not feed final_output: {', '.join(inactive)}")
        result["active_node_ids"] = [node_id for node_id in node_types if node_id in active]
        active_layers = [[node_id for node_id in layer if node_id in active] for layer in layers]
        result["layers"] = [layer for layer in active_layers if layer]
    result["valid"] = not errors
    return result


def ordered_upstream_ids(node, workflow):
    """Return direct input IDs in inspector order, followed by edge order."""
    sources = list(dict.fromkeys(
        edge["source"] for edge in workflow["edges"] if edge["target"] == node["id"]
    ))
    requested_order = node_config(node).get("input_order", [])
    ordered = dict.fromkeys(source for source in requested_order if source in sources)
    ordered.update(dict.fromkeys(sources))
    return list(ordered)


def _upstream_text(output):
    if isinstance(output, Mapping):
        output = output.get("output_text", output.get("text", ""))
    return "" if output is None else str(output)


def _history_text(turn):
    if isinstance(turn, Mapping):
        user = turn.get("user_message", turn.get("user", ""))
        assistant = turn.get("final_response", turn.get("assistant", ""))
        return f"User: {user or ''}\nAssistant: {assistant or ''}"
    return str(turn)


def build_node_prompt(node, workflow, upstream_outputs, conversation_history, *, max_context_chars, provider=None):
    """Build a node prompt from its current inputs and the selected conversation turns."""
    config = node_config(node)
    sections = []
    if config.get("system_prompt"):
        sections.append(f"[System prompt]\n{config['system_prompt']}")
    nodes = {item["id"]: item for item in workflow["nodes"]}
    upstream_inputs = []
    for source in ordered_upstream_ids(node, workflow):
        if source not in upstream_outputs:
            continue
        source_node = nodes[source]
        label = source_node.get("label") or source_node.get("name") or source
        text = _upstream_text(upstream_outputs[source])
        upstream_inputs.append({"node_id": source, "label": label, "text": text})
        sections.append(f"[Upstream: {label}]\n{text}")
    history_count = config.get("history_turns", 0) if config.get("include_history") else 0
    history = list(conversation_history)[-history_count:] if history_count else []
    if history:
        sections.append("[Current conversation history]\n" + "\n\n".join(map(_history_text, history)))
    limit = min(max_context_chars, 14000) if provider == "agy_cli" else max_context_chars
    prompt, truncated = truncate_context("\n\n".join(sections), limit)
    return {
        "prompt": prompt,
        "upstream_inputs": upstream_inputs,
        "history_included": len(history),
        "truncated": truncated,
    }
