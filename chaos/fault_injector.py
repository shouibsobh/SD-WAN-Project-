import json
import subprocess
import time

from telemetry.database import DatabaseManager
from telemetry.ryu_client import RyuClient
from telemetry.topology_manager import TopologyManager


class FaultInjector:
    """Impairs real switch interfaces with tc netem / ip link."""

    def __init__(self):
        self.client = RyuClient()
        self.topology = TopologyManager()
        self.db = DatabaseManager()

        # (switch_id, port_no) -> OS interface name, e.g. "s1-eth2"
        self._port_names = {}

    # -- topology / interface resolution --------------------------------

    def _refresh(self):
        """Re-read the topology and the switch -> interface name mapping."""
        self.topology.discover()
        self._port_names = {}

        for switch in self.client.get_switches():
            desc = self.client.get_port_description(switch)
            if desc is None:
                continue
            for port in desc[str(switch)]:
                if port["port_no"] != "LOCAL":
                    self._port_names[(switch, port["port_no"])] = port["name"]

    def _interfaces_for_link(self, switch_a, switch_b):
        data = self.topology.graph.get_edge_data(f"s{switch_a}", f"s{switch_b}")
        if data is None or data.get("dst_switch") is None:
            return None, None

        # The edge is stored in one direction only, so the ports may be swapped.
        if data["src_switch"] == switch_a:
            port_a, port_b = data["src_port"], data["dst_port"]
        else:
            port_a, port_b = data["dst_port"], data["src_port"]

        return (self._port_names.get((switch_a, port_a)),
                self._port_names.get((switch_b, port_b)))

    def _resolve(self, switch_a, switch_b):
        """_refresh + _interfaces_for_link, with the not-found message."""
        self._refresh()
        iface_a, iface_b = self._interfaces_for_link(switch_a, switch_b)
        if iface_a is None or iface_b is None:
            print(f"[chaos] link s{switch_a}<->s{switch_b} is not in the topology")
        return iface_a, iface_b

    # -- shell + logging ------------------------------------------------

    def _run(self, cmd):
        print("[tc]", " ".join(cmd))
        # Called from the dashboard's request thread: a sudo that decides to
        # prompt for a password would block forever with no one on stdin.
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError) as exc:
            print("[tc] failed:", exc)
            return False

        if result.returncode != 0:
            print("[tc] failed:", result.stderr.strip())
        return result.returncode == 0

    def _log_fault(self, switch_a, switch_b, fault_type, parameters, result):
        """One chaos_log row per action.

        performance_report.py pairs each "injected" with the following
        "cleared" on the same switch pair to get the true fault start/end,
        independent of when the agent noticed. The dashboard shades the
        same pairs. A failed insert must not abort a chaos action, so it
        only costs this one incident its report row.
        """
        try:
            self.db.insert_chaos_event(
                switch_a, switch_b, fault_type,
                json.dumps(parameters, default=str), result,
            )
        except Exception as exc:
            print(f"[chaos] failed to log chaos_log event: {exc}")

    # -- soft impairment: delay / jitter / loss / rate -------------------

    @staticmethod
    def _netem_args(delay_ms, jitter_ms, loss_percent, rate_mbit):
        args = []
        if delay_ms is not None:
            args += ["delay", f"{delay_ms}ms"]
            if jitter_ms is not None:
                args += [f"{jitter_ms}ms"]          # jitter only reads as jitter after a delay
        if loss_percent is not None:
            args += ["loss", f"{loss_percent}%"]
        if rate_mbit is not None:
            args += ["rate", f"{rate_mbit}mbit"]
        return args

    def inject_link_fault(self, switch_a, switch_b, delay_ms=None, jitter_ms=None,
                          loss_percent=None, rate_mbit=None):
        iface_a, iface_b = self._resolve(switch_a, switch_b)
        if iface_a is None:
            return False

        netem = self._netem_args(delay_ms, jitter_ms, loss_percent, rate_mbit)
        if not netem:
            print("[chaos] no impairment specified, nothing to inject")
            return False

        # Both ends, otherwise only one direction of the path is degraded.
        head = ["sudo", "tc", "qdisc", "replace", "dev"]
        ok_a = self._run(head + [iface_a, "root", "netem"] + netem)
        ok_b = self._run(head + [iface_b, "root", "netem"] + netem)

        if ok_a and ok_b:
            self._log_fault(
                switch_a, switch_b, "netem",
                {"delay_ms": delay_ms, "jitter_ms": jitter_ms,
                 "loss_percent": loss_percent, "rate_mbit": rate_mbit},
                "injected",
            )
        return ok_a and ok_b

    def clear_link_fault(self, switch_a, switch_b):
        iface_a, iface_b = self._resolve(switch_a, switch_b)
        if iface_a is None:
            return False

        # Deleting an absent qdisc returns non-zero; harmless, so ignore it.
        self._run(["sudo", "tc", "qdisc", "del", "dev", iface_a, "root"])
        self._run(["sudo", "tc", "qdisc", "del", "dev", iface_b, "root"])

        self._log_fault(switch_a, switch_b, "netem", {}, "cleared")
        return True

    def clear_all_faults(self):
        self._refresh()
        cleared = 0

        for _, _, data in self.topology.graph.edges(data=True):
            if data.get("dst_switch") is None:
                continue                            # host-facing edge
            self.clear_link_fault(data["src_switch"], data["dst_switch"])
            cleared += 1

        print(f"[chaos] cleared netem on {cleared} link(s)")

    def inject_temporary_fault(self, switch_a, switch_b, duration_s, **netem_kwargs):
        """Blocks for duration_s -- do not call this from a UI thread."""
        if not self.inject_link_fault(switch_a, switch_b, **netem_kwargs):
            return False

        print(f"[chaos] fault active on s{switch_a}<->s{switch_b} for {duration_s}s")
        time.sleep(duration_s)
        self.clear_link_fault(switch_a, switch_b)
        print(f"[chaos] fault cleared on s{switch_a}<->s{switch_b}")
        return True

    # -- hard failure: interface up/down --------------------------------

    def set_link_state(self, switch_a, switch_b, up):
        iface_a, iface_b = self._resolve(switch_a, switch_b)
        if iface_a is None:
            return False

        state = "up" if up else "down"
        ok_a = self._run(["sudo", "ip", "link", "set", iface_a, state])
        ok_b = self._run(["sudo", "ip", "link", "set", iface_b, state])

        if ok_a and ok_b:
            self._log_fault(
                switch_a, switch_b, "link_state", {"state": state},
                "cleared" if up else "injected",
            )
        return ok_a and ok_b