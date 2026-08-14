
import networkx as nx

from telemetry.ryu_client import RyuClient
from telemetry import config


class TopologyManager:

    def __init__(self):
        self.client = RyuClient()
        self.graph = nx.Graph()

    def discover(self):
        """Rebuild the graph from scratch. Cheap enough to call per poll."""
        self.graph.clear()

        for sw in self.client.get_switches():
            self.graph.add_node(f"s{sw}", type="switch")

        for link in self.client.get_links():
            src, dst = link["src"], link["dst"]

            src_sw = int(src["dpid"], 16)
            dst_sw = int(dst["dpid"], 16)

            # Undirected, so the reverse link folds into the same edge.
            self.graph.add_edge(
                f"s{src_sw}", f"s{dst_sw}",
                src_switch=src_sw, src_port=int(src["port_no"], 16),
                dst_switch=dst_sw, dst_port=int(dst["port_no"], 16),
            )

        for host in self.client.get_hosts():
            ip = self._host_ip(host)
            if ip is None:
                continue

            sw = int(host["port"]["dpid"], 16)

            self.graph.add_node(f"d{ip.split('.')[-1]}", type="host", ip=ip)

            # dst_* stay None: the far end is a host, not a switch port.
            self.graph.add_edge(
                f"d{ip.split('.')[-1]}", f"s{sw}",
                src_switch=sw, src_port=int(host["port"]["port_no"], 16),
                dst_switch=None, dst_port=None,
            )

    @staticmethod
    def _host_ip(host):
       #The real host IPv4 behind a Ryu host entry, or None to skip it.
        for ip in host.get("ipv4") or []:
            if ip == "0.0.0.0":
                continue
            if ip.startswith(f"{config.TUNNEL_SUBNET_PREFIX}."):
                continue
            return ip

        return None

    def get_candidate_switch_paths(self, switch_a, switch_b, max_paths=5):
        #Up to max_paths underlay routes between two dpids, shortest first.

        src, dst = f"s{switch_a}", f"s{switch_b}"

        underlay = self.graph.subgraph(
            n for n, d in self.graph.nodes(data=True) if d.get("type") == "switch"
        )

        if src not in underlay or dst not in underlay:
            return []

        try:
            paths = []
            for path in nx.shortest_simple_paths(underlay, src, dst):
                paths.append(path)
                if len(paths) >= max_paths:
                    break
            return paths
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []
