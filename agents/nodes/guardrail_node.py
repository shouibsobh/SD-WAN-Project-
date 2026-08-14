import json

from telemetry.topology_manager import TopologyManager
from agents.state import GraphState
from agents.nodes.active_path import get_active_path, target_identity

_topology = TopologyManager()


def _check_reroute(target, candidate_paths, metric_type):
    selected_path = target.get("selected_path")

    if target.get("switch_a") is None or target.get("switch_b") is None:
        return False, None, (
            "REROUTE proposed without a valid target identity "
            "(need switch_a/switch_b)"
        )

    if not candidate_paths:
        # Fail closed: nothing was verified, so nothing gets approved.
        return False, None, (
            "No candidate paths were available to choose from; "
            "cannot approve a REROUTE without a verified option"
        )

    if not selected_path:
        return False, None, "REROUTE proposed without selected_path in target"

    if selected_path not in candidate_paths:
        return False, None, (
            f"selected_path {selected_path} is not one of the candidate "
            f"paths offered -- rejecting (stale topology reference)"
        )

    # decision_node already keeps the active path out of the REROUTE
    # options, so this should not fire. Kept because the two nodes read
    # the network at different moments: if a reroute landed in between,
    # this is what catches it.
    active_path = get_active_path(
        metric_type, target, candidate_paths, _topology
    )

    if selected_path == active_path:
        return False, selected_path, (
            "Selected path became the active one between the decision and "
            "this check -- rerouting would change nothing"
        )

    return True, selected_path, "Selected path verified among candidates"


def guardrail_node(state: GraphState) -> dict:
    """Approve or reject the proposed action against the live network."""
    proposed = state.get("proposed_action")

    # Copy the incoming state so metric / rl_transition_id survive.
    result = dict(state)

    if proposed is None or proposed.get("action") == "NONE":
        result.update({
            "approved": False,
            "guardrail_reason": "No action was proposed",
        })
        return result

    action = proposed["action"]

    if action != "REROUTE":
        result.update({
            "approved": False,
            "guardrail_reason": f"Unrecognized action {action!r}",
        })
        return result

    approved, candidate_path, reason = _check_reroute(
        proposed.get("target") or {},
        state.get("candidate_paths"),
        state.get("metric_type"),
    )

    result.update({
        "approved": approved,
        "guardrail_reason": reason,
        "candidate_path": candidate_path if approved else None,
    })

    return result
