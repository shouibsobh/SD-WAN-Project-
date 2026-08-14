import json
import os
import time

from telemetry.topology_manager import TopologyManager
from telemetry.database import DatabaseManager
from agents.state import AgentState
from agents.nodes.active_path import get_active_path
from agents.nodes.metrics_provider import (
    get_path_features, get_tunnel_sla, tunnel_sla_vector,
)
from agents.rl.path_scorer import PathScorer

MAX_CANDIDATE_PATHS = 5


EPSILON = float(os.environ.get("RL_EPSILON", 0.1))

_topology = TopologyManager()
_db = DatabaseManager()
_scorer = PathScorer.load_or_new()


def get_scorer():

    return _scorer


def _fallback(reason, metric=None, metric_type=None, candidate_paths=None):

    return {
        "metric": metric,
        "metric_type": metric_type,

        "reasoning": reason,
        "proposed_action": {
            "action": "NONE",
            "target": {},
            "reasoning": reason,
        },
        "candidate_paths": candidate_paths,
    }


def _score_candidate_paths(candidate_paths, target_extra):

    options = []

    for path in candidate_paths:
        options.append({
            "action": "REROUTE",
            "target": {**target_extra, "selected_path": path},
            "features": get_path_features(path, _topology),
        })

    return options


def _stay_option(active_path):

    features = (
        get_path_features(active_path, _topology)
        if active_path else None      # unknown: fall back to the flat baseline
    )

    return {"action": "NONE", "target": {}, "features": features}


def _build_tunnel_reroute_options(switch_a, switch_b, metric_type):

    candidate_paths = []

    try:
        _topology.discover()
        if switch_a is not None and switch_b is not None:
            candidate_paths = _topology.get_candidate_switch_paths(
                switch_a, switch_b, max_paths=MAX_CANDIDATE_PATHS
            )
    except Exception:
        candidate_paths = []

    target_extra = {"switch_a": switch_a, "switch_b": switch_b}

    try:
        active_path = get_active_path(
            metric_type, target_extra, candidate_paths, _topology
        )
    except Exception:
        # Unresolvable active path is not fatal -- offer everything and
        # let the guardrail catch a no-op.
        active_path = None

    alternatives = [p for p in candidate_paths if p != active_path]

    options = _score_candidate_paths(alternatives, target_extra)
    options.append(_stay_option(active_path))

    return options, candidate_paths


def _sla_before(metric, switch_a, switch_b):

    if any(metric.get(k) is not None for k in
           ("latency_ms", "jitter_ms", "packet_loss_percent")):
        sla = metric
    else:
        sla = get_tunnel_sla(switch_a, switch_b)

    return tunnel_sla_vector(sla)


def decision_node(state: AgentState) -> dict:

    metric = state["metric"]
    metric_type = state["metric_type"]
    anomaly_state = metric.get("state", "UNKNOWN")

    candidate_paths = []
    options = []
    switch_a = switch_b = None

    if metric_type == "tunnel":

        switch_a = metric.get("switch_a")
        switch_b = metric.get("switch_b")

        options, candidate_paths = _build_tunnel_reroute_options(
            switch_a, switch_b, metric_type
        )

    # A lone NONE means every candidate was the active path, so there is
    # nowhere else to go.
    if not options or all(o["action"] == "NONE" for o in options):
        return _fallback(
            reason=(
                "No alternative path exists to reroute onto "
                "(no candidate paths, unsupported metric_type, or the "
                "only candidate is the one already active)"
            ),
            metric=metric,
            metric_type=metric_type,
            candidate_paths=candidate_paths,
        )

    chosen, score, was_exploration = _scorer.select(options, epsilon=EPSILON)

    if chosen["action"] == "NONE":

        reasoning = (
            f"RL scorer chose to stay on the active path "
            f"(score {score:.3f} beat every alternative)"
        )

        return _fallback(
            reason=reasoning,
            metric=metric,
            metric_type=metric_type,
            candidate_paths=candidate_paths,
        )

    reasoning = (
        f"RL scorer picked {chosen['action']} "
        f"(score={score:.3f}, "
        f"{'exploration' if was_exploration else 'exploitation'})"
    )

    proposed_action = {
        "action": chosen["action"],
        "target": chosen["target"],
        "reasoning": reasoning,
    }

    transition_id = _db.insert_rl_transition({
        "timestamp": time.time(),
        "metric_type": metric_type,
        "anomaly_state": anomaly_state,
        "action": chosen["action"],
        "target": json.dumps(chosen["target"], default=str),
        "features": json.dumps(chosen["features"]),
        "sla_before": json.dumps(_sla_before(metric, switch_a, switch_b)),
    })

    return {
        "metric": metric,
        "metric_type": metric_type,

        "reasoning": reasoning,
        "proposed_action": proposed_action,
        "candidate_paths": candidate_paths,
        "rl_transition_id": transition_id,
    }