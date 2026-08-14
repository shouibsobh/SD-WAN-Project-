from telemetry.ryu_client import RyuClient
from telemetry.topology_manager import TopologyManager
from telemetry.database import DatabaseManager
from agents.state import GraphState

# Above the controller's FLOW_PRIORITY_FORWARD (10) so the reroute wins.
OVERRIDE_FLOW_PRIORITY = 100
OVERRIDE_IDLE_TIMEOUT_S = 300   # raised from 60: 20x SLA_POLL_INTERVAL, so
                                # active probes keep it refreshed with slack
_client = RyuClient()
_topology = TopologyManager()
_db = DatabaseManager()


def _resolve_hop_port(topology, u, v):
    """Port on u that leads to v (edge may store either end as src)."""
    data = topology.graph.get_edge_data(u, v)

    if data is None:
        return None

    if data.get("src_switch") is not None and f"s{data['src_switch']}" == u:
        return data["src_port"]

    if data.get("dst_switch") is not None and f"s{data['dst_switch']}" == u:
        return data["dst_port"]

    return None


def _install_flows_for_mac(path, dst_mac):
    installed_on = []

    for u, v in zip(path, path[1:]):
        if not u.startswith("s"):
            continue                        # host hop, not an OpenFlow switch

        out_port = _resolve_hop_port(_topology, u, v)
        if out_port is None:
            return False, installed_on, (
                f"Could not resolve outgoing port from {u} to {v} "
                f"-- aborting before installing a partial route"
            )

        ok = _client.add_flow_entry(
            dpid=int(u[1:]),
            match={"eth_dst": dst_mac},
            actions=[{"type": "OUTPUT", "port": out_port}],
            priority=OVERRIDE_FLOW_PRIORITY,
            idle_timeout=OVERRIDE_IDLE_TIMEOUT_S,
        )

        if not ok:
            return False, installed_on, (
                f"Failed to install flow on {u} (installed OK on "
                f"{installed_on} before this failure -- network is now "
                f"partially rerouted, consider manual cleanup)"
            )

        installed_on.append(u)

    return True, installed_on, None


def _install_tunnel_reroute(path, switch_a, switch_b):
    try:
        _topology.discover()
    except Exception as exc:
        return False, f"Could not fetch live topology to install reroute: {exc}"

    tunnels = {
        (t["switch_a"], t["switch_b"]): t for t in _db.get_tunnels()
    }
    tunnel = tunnels.get((min(switch_a, switch_b), max(switch_a, switch_b)))

    if tunnel is None:
        return False, (
            f"No tunnel registry entry for s{switch_a}<->s{switch_b} "
            f"-- was it removed by TunnelManager since this decision?"
        )

    mac_a = _client.get_ip_mac(tunnel["remote_ip_a"])
    mac_b = _client.get_ip_mac(tunnel["remote_ip_b"])

    if mac_a is None or mac_b is None:
        return False, (
            f"Could not resolve tunnel endpoint MAC(s) yet "
            f"(s{switch_a}={tunnel['remote_ip_a']}->{mac_a}, "
            f"s{switch_b}={tunnel['remote_ip_b']}->{mac_b}) -- needs at "
            f"least one probe (ping) to have crossed this tunnel already"
        )

    ok_fwd, installed_fwd, err_fwd = _install_flows_for_mac(path, mac_b)
    ok_rev, installed_rev, err_rev = _install_flows_for_mac(
        list(reversed(path)), mac_a
    )

    if not ok_fwd or not ok_rev:
        return False, (
            f"Partially rerouted tunnel s{switch_a}<->s{switch_b}: "
            f"forward {'OK' if ok_fwd else f'FAILED ({err_fwd})'} on "
            f"{installed_fwd}, reverse "
            f"{'OK' if ok_rev else f'FAILED ({err_rev})'} on "
            f"{installed_rev} -- consider manual cleanup"
        )

    return True, (
        f"Rerouted tunnel s{switch_a}<->s{switch_b} onto {path} "
        f"(priority={OVERRIDE_FLOW_PRIORITY}, "
        f"idle_timeout={OVERRIDE_IDLE_TIMEOUT_S}s), "
        f"forward on {installed_fwd}, reverse on {installed_rev}"
    )


def execution_node(state: GraphState) -> dict:
    action = state["proposed_action"]

    if action["action"] != "REROUTE":
        return {
            "executed": False,
            "execution_result": f"Unrecognized action {action['action']!r}",
        }

    candidate_path = state.get("candidate_path")
    target = action.get("target") or {}
    switch_a, switch_b = target.get("switch_a"), target.get("switch_b")

    if not candidate_path:
        return {
            "executed": False,
            "execution_result": "Missing candidate_path, nothing executed",
        }

    if switch_a is None or switch_b is None:
        return {
            "executed": False,
            "execution_result":
                "REROUTE target has no switch_a/switch_b, nothing executed",
        }

    ok, detail = _install_tunnel_reroute(candidate_path, switch_a, switch_b)

    print(f"[EXECUTE][REROUTE] {'OK' if ok else 'FAILED'}: {detail}")
    return {"executed": ok, "execution_result": detail}
