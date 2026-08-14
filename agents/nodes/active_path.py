

import json

from telemetry.database import DatabaseManager
from telemetry.ryu_client import RyuClient
from telemetry import config
from agents.nodes.execution_node import OVERRIDE_FLOW_PRIORITY

_db = DatabaseManager()
_ryu = RyuClient()

MAX_LIVE_PATH_HOPS = 20   # loop guard: a real path never needs this many


def pair(a, b):
    return tuple(sorted((a, b)))


def target_identity(target):
    """Order-independent identity for a tunnel target.

    s2<->s4 is one tunnel whichever end triggered the reroute.
    """
    if target.get("switch_a") is not None and target.get("switch_b") is not None:
        return pair(target["switch_a"], target["switch_b"])

    return None


# -- layer 1: reconstruct the path from live flow tables ---------------

def _out_port_for_override_flow(dpid, mac):
    """OUTPUT port of dpid's installed override flow for `mac`, if any."""
    mac = mac.lower()

    try:
        flows = _ryu.get_flow_stats(dpid)
    except Exception:
        return None

    for flow in flows:
        if flow.get("priority") != OVERRIDE_FLOW_PRIORITY:
            continue

        match = flow.get("match") or {}
        if (match.get("eth_dst") or match.get("dl_dst") or "").lower() != mac:
            continue

        for action in flow.get("actions") or []:

            if isinstance(action, dict) and action.get("type") == "OUTPUT":
                return action.get("port")

            if isinstance(action, str) and action.startswith("OUTPUT"):
                try:
                    return int(action.split(":")[1])
                except (IndexError, ValueError):
                    continue

    return None


def _next_hop_dpid(topology, current_dpid, out_port):
    """Which neighbour sits behind out_port on current_dpid."""
    neighbors = topology.graph.adj.get(f"s{current_dpid}", {})

    # The graph is undirected, so either end may be stored as src.
    for _neighbor, data in neighbors.items():
        if data.get("src_switch") == current_dpid and data.get("src_port") == out_port:
            return data.get("dst_switch")
        if data.get("dst_switch") == current_dpid and data.get("dst_port") == out_port:
            return data.get("src_switch")

    return None


def _reconstruct_live_tunnel_path(switch_a, switch_b, dst_mac, topology):
    """Follow the installed override flows hop by hop from switch_a.

    Mirrors how execution_node._install_flows_for_mac laid them down.
    Returns (path, complete); complete is True only if the chain reached
    switch_b.
    """
    path = [switch_a]
    current = switch_a
    visited = {switch_a}

    for _ in range(MAX_LIVE_PATH_HOPS):
        if current == switch_b:
            return path, True

        out_port = _out_port_for_override_flow(current, dst_mac)
        if out_port is None:
            return path, False              # chain ends early

        next_dpid = _next_hop_dpid(topology, current, out_port)
        if next_dpid is None or next_dpid in visited:
            return path, False              # dead end or loop

        path.append(next_dpid)
        visited.add(next_dpid)
        current = next_dpid

    return path, False


def _get_active_path_live(metric_type, target, topology):

    if metric_type != "tunnel":
        return None

    switch_a, switch_b = target.get("switch_a"), target.get("switch_b")
    if switch_a is None or switch_b is None:
        return None

    # VTEP addressing is deterministic (tunnel_manager._tunnel_ip), so
    # there's no need to look switch_b up in the tunnels table.
    mac_b = _ryu.get_ip_mac(f"{config.TUNNEL_SUBNET_PREFIX}.{switch_b}")
    if mac_b is None:
        # Nothing has crossed the tunnel yet, so no override can exist.
        return None

    dpids, complete = _reconstruct_live_tunnel_path(
        switch_a, switch_b, mac_b, topology
    )
    return [f"s{d}" for d in dpids] if complete else None


# -- layer 2: infer from agent_decisions history ------------------------

def _get_active_path_from_history(metric_type, identity, candidate_paths):

    if not candidate_paths:
        return None

    for decision in _db.get_recent_reroute_decisions(metric_type):
        try:
            target = json.loads(decision["target"])
        except (json.JSONDecodeError, TypeError):
            continue

        if target_identity(target) != identity:
            continue

        active_path = target.get("selected_path")
        if active_path in candidate_paths:
            return active_path

        break   # newest entry is already stale; older ones more so

    return candidate_paths[0]


def get_active_path(metric_type, target, candidate_paths, topology):
    live_path = _get_active_path_live(metric_type, target, topology)
    if live_path is not None:
        return live_path

    return _get_active_path_from_history(
        metric_type, target_identity(target), candidate_paths
    )
