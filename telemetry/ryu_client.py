
import requests

from telemetry import config

DEFAULT_TIMEOUT_S = 5


class RyuClient:

    def __init__(self, host=config.RYU_IP, port=config.RYU_PORT):
        self.base_url = f"http://{host}:{port}"

    def _get(self, path, default=None):
        """GET path, or `default` on any transport/HTTP/decode failure."""
        try:
            response = requests.get(
                f"{self.base_url}{path}", timeout=DEFAULT_TIMEOUT_S
            )
            response.raise_for_status()
            return response.json()
        except (requests.exceptions.RequestException, ValueError):
            return default

    # -- topology REST app --

    def get_switches(self):
        return self._get("/stats/switches", [])

    def get_topology(self):
        return self._get("/v1.0/topology/switches", [])

    def get_links(self):
        return self._get("/v1.0/topology/links", [])

    def get_hosts(self):
        return self._get("/v1.0/topology/hosts", [])

    # -- ofctl_rest --

    def get_port_stats(self, dpid):
        return self._get(f"/stats/port/{dpid}", [])

    def get_port_description(self, switch_id):
        # None, not [], so callers can tell "no answer" from "no ports".
        return self._get(f"/stats/portdesc/{switch_id}")

    def get_flow_stats(self, dpid):
        return (self._get(f"/stats/flow/{dpid}", {}) or {}).get(str(dpid), [])

    def _flowentry(self, verb, body):
        try:
            response = requests.post(
                f"{self.base_url}/stats/flowentry/{verb}",
                json=body, timeout=DEFAULT_TIMEOUT_S,
            )
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException:
            return False

    def add_flow_entry(self, dpid, match, actions,
                       priority=100, idle_timeout=0, hard_timeout=0):
        return self._flowentry("add", {
            "dpid": dpid,
            "priority": priority,
            "idle_timeout": idle_timeout,
            "hard_timeout": hard_timeout,
            "match": match,
            "actions": actions,
        })

    def delete_flow_entry(self, dpid, match, priority=None):
        body = {"dpid": dpid, "match": match}
        if priority is not None:
            body["priority"] = priority
        return self._flowentry("delete", body)

    # -- custom /telemetry endpoints (topology_aware_switch.py) --

    def get_ip_mac(self, ip):
        """MAC the controller learned for `ip`, or None if it hasn't."""
        return (self._get(f"/telemetry/ip_mac/{ip}", {}) or {}).get("mac")

    def get_edge_switches(self):
        return (self._get("/telemetry/edge_switches", {}) or {}).get(
            "edge_switches", []
        )
