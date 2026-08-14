
import subprocess
import time

from telemetry.database import DatabaseManager
from telemetry.ryu_client import RyuClient
from telemetry import config


class TunnelManager:

    def __init__(self):
        self.client = RyuClient()
        self.db = DatabaseManager()

        # dpid -> VTEP IP
        self._endpoint_ip = {}

    # ==============================================================
    # Shell helpers
    # ==============================================================

    def _run(self, cmd, check_ok=True):
        print("[tunnel]", " ".join(cmd))
        return self._exec(cmd, check_ok=check_ok)

    def _exec(self, cmd, check_ok=False, timeout=20):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            print("[tunnel] failed:", exc)
            return subprocess.CompletedProcess(cmd, 124, "", str(exc))

        if check_ok and result.returncode != 0:
            print("[tunnel] failed:", result.stderr.strip())

        return result

    def _run_ns(self, netns, cmd, check_ok=True):
        return self._run(
            ["sudo", "ip", "netns", "exec", netns] + cmd,
            check_ok=check_ok
        )

    # ==============================================================
    # Naming
    # ==============================================================

    def _bridge_name(self, dpid):
        return f"s{dpid}"

    def _netns_name(self, dpid):
        return f"ns-s{dpid}"

    # --------------------------------------------------------------
    # Namespace-local Linux bridge
    # --------------------------------------------------------------

    def _vx_bridge_name(self, dpid):
        return "br-vx"

    # --------------------------------------------------------------
    # VTEP
    # --------------------------------------------------------------

    def _endpoint_iface(self, dpid):
        return f"s{dpid}-vx0"

    def _tunnel_ip(self, dpid):
        return f"{config.TUNNEL_SUBNET_PREFIX}.{dpid}"

    def _root_veth(self, dpid):
        """Root-namespace side of the pair; goes on OVS bridge sX."""
        return f"s{dpid}-vxv"

    def _ns_veth(self, dpid):
        """Namespace side of the pair; goes on br-vx."""
        return f"vxv-s{dpid}"

    def _tunnel_port_name(self, local_dpid, remote_dpid):
        return f"vx-{local_dpid}-{remote_dpid}"[:15]   # IFNAMSIZ

    # ==============================================================
    # Namespace helpers
    # ==============================================================

    def _netns_exists(self, netns):
        result = self._exec(["sudo", "ip", "netns", "list"])

        for line in result.stdout.splitlines():
            line = line.strip()

            if not line:
                continue

            name = line.split()[0]

            if name == netns:
                return True

        return False

    def _ensure_netns(self, dpid):
        netns = self._netns_name(dpid)

        if not self._netns_exists(netns):
            self._run(["sudo", "ip", "netns", "add", netns])

        self._run_ns(netns, ["ip", "link", "set", "lo", "up"], check_ok=False)

    def _iface_in_netns(self, netns, iface):
        result = self._exec(
            ["sudo", "ip", "netns", "exec", netns, "ip", "link", "show", iface]
        )

        return result.returncode == 0

    def _link_exists_root(self, iface):
        result = self._exec(["sudo", "ip", "link", "show", iface])

        return result.returncode == 0

    # ==============================================================
    # Namespace-local bridge
    # ==============================================================

    def _ensure_namespace_bridge(self, dpid):
        """br-vx inside ns-sX, joining sX-vx0, the ns veth and the VXLANs."""
        netns = self._netns_name(dpid)
        bridge = self._vx_bridge_name(dpid)

        result = self._run_ns(
            netns, ["ip", "link", "show", bridge], check_ok=False
        )

        if result.returncode != 0:
            self._run_ns(
                netns, ["ip", "link", "add", "name", bridge, "type", "bridge"]
            )

        self._run_ns(netns, ["ip", "link", "set", bridge, "up"])

    # ==============================================================
    # Veth boundary
    # ==============================================================

    def _ensure_veth_pair(self, dpid):
        """sX-vxv in root (on OVS sX) <-> vxv-sX in ns-sX (on br-vx)."""
        netns = self._netns_name(dpid)
        ovs_bridge = self._bridge_name(dpid)

        root_veth = self._root_veth(dpid)
        ns_veth = self._ns_veth(dpid)
        vx_bridge = self._vx_bridge_name(dpid)

        root_exists = self._link_exists_root(root_veth)
        ns_exists = self._iface_in_netns(netns, ns_veth)

        if root_exists and ns_exists:
            self._attach_root_veth_to_ovs(
                dpid,
                root_veth
            )

            self._attach_ns_veth_to_bridge(
                dpid,
                ns_veth
            )

            return True

        # Half-built from an interrupted run: tear down and redo.
        if root_exists or ns_exists:
            self._remove_veth_pair(dpid)

        result = self._run([
            "sudo", "ip", "link", "add", root_veth,
            "type", "veth", "peer", "name", ns_veth,
        ])

        if result.returncode != 0:
            return False

        result = self._run(
            ["sudo", "ip", "link", "set", ns_veth, "netns", netns]
        )

        if result.returncode != 0:
            self._run(["sudo", "ip", "link", "del", root_veth], check_ok=False)
            return False

        self._run(["sudo", "ip", "link", "set", root_veth, "up"])
        self._run_ns(netns, ["ip", "link", "set", ns_veth, "up"])

        if not self._attach_root_veth_to_ovs(dpid, root_veth):
            self._remove_veth_pair(dpid)
            return False

        if not self._attach_ns_veth_to_bridge(dpid, ns_veth):
            self._remove_veth_pair(dpid)
            return False

        return True

    def _attach_root_veth_to_ovs(self, dpid, root_veth):
        bridge = self._bridge_name(dpid)

        # Already on a bridge -- nothing to do.
        if self._exec(["sudo", "ovs-vsctl", "port-to-br", root_veth]).returncode == 0:
            return True

        result = self._run(["sudo", "ovs-vsctl", "add-port", bridge, root_veth])

        return result.returncode == 0

    def _attach_ns_veth_to_bridge(self, dpid, ns_veth):
        netns = self._netns_name(dpid)
        bridge = self._vx_bridge_name(dpid)

        result = self._run_ns(
            netns, ["ip", "link", "set", ns_veth, "master", bridge],
            check_ok=False,
        )

        if result.returncode != 0:
            # May already be attached; the bridge listing settles it.
            check = self._run_ns(
                netns, ["bridge", "link", "show"], check_ok=False
            )

            if ns_veth not in check.stdout:
                print(f"[tunnel] failed attaching {ns_veth} to {bridge}")
                return False

        return True

    def _remove_veth_pair(self, dpid):
        root_veth = self._root_veth(dpid)
        netns = self._netns_name(dpid)
        ns_veth = self._ns_veth(dpid)

        self._run(
            ["sudo", "ovs-vsctl", "del-port",
             self._bridge_name(dpid), root_veth],
            check_ok=False,
        )

        self._run_ns(netns, ["ip", "link", "del", ns_veth], check_ok=False)

        self._run([
            "sudo",
            "ip",
            "link",
            "del",
            root_veth
        ], check_ok=False)

    # ==============================================================
    # VTEP endpoint
    # ==============================================================

    def _ensure_endpoint(self, dpid):
        """Build ns-sX's VTEP: br-vx holding 10.200.0.X, with sX-vx0
        and vxv-sX enslaved to it.

        The VTEP is not an OVS port -- it lives entirely in ns-sX.
        """

        if dpid in self._endpoint_ip:
            return self._endpoint_ip[dpid]

        bridge = self._bridge_name(dpid)
        netns = self._netns_name(dpid)
        iface = self._endpoint_iface(dpid)
        ip = self._tunnel_ip(dpid)
        vx_bridge = self._vx_bridge_name(dpid)

        self._ensure_netns(dpid)
        self._ensure_namespace_bridge(dpid)

        if not self._ensure_veth_pair(dpid):
            raise RuntimeError(f"Could not create veth boundary for s{dpid}")

        # Created inside the namespace -- no ovs-vsctl add-port.
        if not self._iface_in_netns(netns, iface):
            result = self._run_ns(
                netns, ["ip", "link", "add", iface, "type", "dummy"]
            )

            if result.returncode != 0:
                raise RuntimeError(f"Could not create VTEP interface {iface}")

        self._run_ns(
            netns, ["ip", "link", "set", iface, "master", vx_bridge],
            check_ok=False,
        )

        self._run_ns(
            netns, ["ip", "addr", "flush", "dev", vx_bridge], check_ok=False
        )
        self._run_ns(netns, ["ip", "addr", "add", f"{ip}/24", "dev", vx_bridge])

        self._run_ns(netns, ["ip", "link", "set", iface, "up"])
        self._run_ns(netns, ["ip", "link", "set", vx_bridge, "up"])
        self._run_ns(netns, ["ip", "link", "set", "lo", "up"], check_ok=False)

        self._endpoint_ip[dpid] = ip

        return ip

    # ==============================================================
    # VNI
    # ==============================================================

    def _vni_for_pair(self, dpid_a, dpid_b):

        lo, hi = sorted((int(dpid_a), int(dpid_b)))

        return config.TUNNEL_VNI_BASE + lo * 1000 + hi

    def _directional_vni(self, local_dpid, remote_dpid):
        """The real Linux VXLAN VNI: s1->s2 = 11002, s2->s1 = 12001.

        Directional so creating both ends can't collide in the kernel.
        """
        return (
            config.TUNNEL_VNI_BASE
            + int(local_dpid) * 1000
            + int(remote_dpid)
        )

    # ==============================================================
    # VXLAN device helpers
    # ==============================================================

    def _kernel_vxlan_exists(self, netns, iface):
        return self._iface_in_netns(netns, iface)

    def _create_kernel_vxlan(self, local_dpid, remote_dpid,
                             local_ip, remote_ip):
        """Create the VXLAN device inside ns-sX and enslave it to br-vx.

        Never added to OVS, never moved out of the namespace.
        """

        netns = self._netns_name(local_dpid)
        bridge = self._vx_bridge_name(local_dpid)
        iface = self._tunnel_port_name(local_dpid, remote_dpid)
        vni = self._directional_vni(local_dpid, remote_dpid)

        if self._kernel_vxlan_exists(netns, iface):
            return True

        result = self._run_ns(netns, [
            "ip", "link", "add", iface, "type", "vxlan",
            "id", str(vni),
            "local", local_ip,
            "remote", remote_ip,
            "dstport", str(config.VXLAN_UDP_PORT),
            "nolearning",
        ])

        if result.returncode != 0:
            return False

        for step in (
            ["ip", "link", "set", iface, "master", bridge],
            ["ip", "link", "set", iface, "up"],
        ):
            if self._run_ns(netns, step).returncode != 0:
                self._run_ns(
                    netns, ["ip", "link", "del", iface], check_ok=False
                )
                return False

        print(f"[tunnel] kernel VXLAN created ns={netns} {iface} "
              f"vni={vni} {local_ip}->{remote_ip}")

        return True

    # ==============================================================
    # Tunnel creation
    # ==============================================================

    def _create_tunnel(self, dpid_a, dpid_b):

        dpid_a, dpid_b = sorted((int(dpid_a), int(dpid_b)))

        print(f"[tunnel] creating VXLAN s{dpid_a}<->s{dpid_b}")

        ip_a = self._ensure_endpoint(dpid_a)
        ip_b = self._ensure_endpoint(dpid_b)

        logical_vni = self._vni_for_pair(dpid_a, dpid_b)

        ok_a = self._create_kernel_vxlan(
            local_dpid=dpid_a, remote_dpid=dpid_b,
            local_ip=ip_a, remote_ip=ip_b,
        )
        ok_b = self._create_kernel_vxlan(
            local_dpid=dpid_b, remote_dpid=dpid_a,
            local_ip=ip_b, remote_ip=ip_a,
        )

        if not (ok_a and ok_b):
            print(f"[tunnel] FAILED s{dpid_a}<->s{dpid_b} "
                  f"(A={'OK' if ok_a else 'FAILED'}, "
                  f"B={'OK' if ok_b else 'FAILED'})")

            self._remove_tunnel(dpid_a, dpid_b)
            return False

        self.db.upsert_tunnel(
            switch_a=dpid_a, switch_b=dpid_b, vni=logical_vni,
            port_a=None, port_b=None,
            remote_ip_a=ip_a, remote_ip_b=ip_b,
        )

        print(f"[tunnel] CREATED s{dpid_a}<->s{dpid_b} "
              f"logical-vni={logical_vni} directional-vni="
              f"{self._directional_vni(dpid_a, dpid_b)}/"
              f"{self._directional_vni(dpid_b, dpid_a)}")

        return True

    # ==============================================================
    # Tunnel removal
    # ==============================================================

    def _delete_kernel_vxlan(self, local_dpid, remote_dpid):
        self._run_ns(
            self._netns_name(local_dpid),
            ["ip", "link", "del", self._tunnel_port_name(local_dpid, remote_dpid)],
            check_ok=False,
        )

    def _remove_tunnel(self, dpid_a, dpid_b):

        dpid_a, dpid_b = sorted((int(dpid_a), int(dpid_b)))

        self._delete_kernel_vxlan(dpid_a, dpid_b)
        self._delete_kernel_vxlan(dpid_b, dpid_a)

        self.db.delete_tunnel(dpid_a, dpid_b)

        print(f"[tunnel] removed VXLAN s{dpid_a}<->s{dpid_b}")

    # ==============================================================
    # Edge switch discovery
    # ==============================================================

    def _discover_edge_switches(self):
        return {int(dpid) for dpid in self.client.get_edge_switches()}

    def _send_gratuitous_arp(self, dpid):
        """Nudge the controller's proxy-ARP table. Optional for VXLAN."""
        netns = self._netns_name(dpid)
        # br-vx, not sX-vx0: the address lives on the bridge.
        iface = self._vx_bridge_name(dpid)
        ip = self._tunnel_ip(dpid)

        self._run_ns(
            netns, ["arping", "-c", "2", "-U", "-I", iface, ip],
            check_ok=False,
        )

    # ==============================================================
    # Full mesh
    # ==============================================================

    def ensure_full_mesh(self):

        edge_dpids = self._discover_edge_switches()

        print("[tunnel] edge switches:", sorted(edge_dpids))

        # Endpoints before tunnels: _create_tunnel needs both VTEPs.
        for dpid in sorted(edge_dpids):
            try:
                self._ensure_endpoint(dpid)
                self._send_gratuitous_arp(dpid)
            except Exception as exc:
                print(f"[tunnel] endpoint setup failed for s{dpid}: {exc}")

        existing_pairs = {
            tuple(sorted((int(t["switch_a"]), int(t["switch_b"]))))
            for t in self.db.get_tunnels()
        }

        wanted_pairs = {
            tuple(sorted((a, b)))
            for a in edge_dpids for b in edge_dpids if a != b
        }

        for a, b in sorted(wanted_pairs - existing_pairs):
            try:
                self._create_tunnel(a, b)
            except Exception as exc:
                print(f"[tunnel] failed creating s{a}<->s{b}: {exc}")

        # Tear down tunnels to switches that are no longer edges.
        for a, b in sorted(existing_pairs):
            if a not in edge_dpids or b not in edge_dpids:
                self._remove_tunnel(a, b)

    def run(self):

        print("[tunnel] TunnelManager started")

        # The topology is rebuilt whenever Mininet starts, so rows from
        # a previous run describe interfaces that no longer exist.
        self.db.clear_tunnels()

        while True:
            try:
                self.ensure_full_mesh()
            except Exception as exc:
                print(f"[tunnel] reconciliation error: {exc}")

            time.sleep(config.TUNNEL_POLL_INTERVAL)


if __name__ == "__main__":
    TunnelManager().run()