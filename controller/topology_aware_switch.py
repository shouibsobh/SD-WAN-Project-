import json as json_lib
import re

import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    DEAD_DISPATCHER,
    set_ev_cls,
)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp, ipv4
from ryu.topology import event
from ryu.topology.api import get_switch, get_link
from ryu.app.wsgi import WSGIApplication, ControllerBase, route
from webob import Response


FLOW_IDLE_TIMEOUT = 300
FLOW_PRIORITY_FORWARD = 10
FLOW_PRIORITY_TABLE_MISS = 0

TELEMETRY_INSTANCE_NAME = "topology_aware_switch_app"


_VXLAN_ROOT_VETH_RE = re.compile(r"^s\d+-vxv$")

_VXLAN_TUNNEL_PORT_RE = re.compile(r"^vx-\d+-\d+$")
_VXLAN_ENDPOINT_PORT_RE = re.compile(r"^s\d+-vx0$")


class TopologyAwareSwitch(app_manager.RyuApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # WSGI context so the REST endpoints below can ride Ryu's own server
    # (port 8080, same one ofctl_rest uses -- telemetry/ryu_client.py).
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):

        super().__init__(*args, **kwargs)

        self.datapaths = {}

        # Network topology graph
        self.net = nx.DiGraph()

        # Switch ports connected to other switches
        self.switch_link_ports = {}

        # Ports allowed for safe flooding
        self.tree_ports = {}

        # Host-connected ports
        self.host_ports = {}

        # MAC address location table
        self.mac_to_dpid_port = {}

        # IP-MAC mapping for proxy ARP
        self.ip_to_mac = {}

        wsgi = kwargs["wsgi"]
        wsgi.register(
            TelemetryRestController,
            {TELEMETRY_INSTANCE_NAME: self},
        )

    # -- datapath bookkeeping ---------------------------------------------

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):

        datapath = ev.datapath

        if ev.state == MAIN_DISPATCHER:
            self.datapaths[datapath.id] = datapath

        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(datapath.id, None)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):

        datapath = ev.msg.datapath

        self.datapaths[datapath.id] = datapath

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Table-miss: send anything unmatched up to the controller.
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(
            ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER
        )]

        self._add_flow(
            datapath,
            FLOW_PRIORITY_TABLE_MISS,
            match,
            actions
        )

    # -- topology tracking (event-driven) ---------------------------------

    @set_ev_cls(event.EventSwitchEnter)
    def _switch_enter(self, ev):
        self._update_topology()

    @set_ev_cls(event.EventSwitchLeave)
    def _switch_leave(self, ev):
        self._update_topology()

    @set_ev_cls(event.EventLinkAdd)
    def _link_add(self, ev):
        self._update_topology()

    @set_ev_cls(event.EventLinkDelete)
    def _link_delete(self, ev):
        self._update_topology()

    def _update_topology(self):

        switch_list = get_switch(self, None)
        link_list = get_link(self, None)

        self.net = nx.DiGraph()
        self.switch_link_ports = {}

        for switch in switch_list:
            self.net.add_node(switch.dp.id)

        for link in link_list:
            self.net.add_edge(
                link.src.dpid,
                link.dst.dpid,
                port=link.src.port_no
            )
            self.switch_link_ports.setdefault(
                link.src.dpid, set()
            ).add(link.src.port_no)

        # Build software spanning tree
        undirected = self.net.to_undirected()
        mst = nx.minimum_spanning_tree(undirected)

        self.tree_ports = {dpid: set() for dpid in self.net.nodes}

        for u, v in mst.edges():
            if self.net.has_edge(u, v):
                self.tree_ports[u].add(self.net[u][v]["port"])
            if self.net.has_edge(v, u):
                self.tree_ports[v].add(self.net[v][u]["port"])

        self.logger.info(
            "[topology] %d switches, %d directed links",
            self.net.number_of_nodes(),
            self.net.number_of_edges()
        )

    # -- port classification ----------------------------------------------

    def _port_name(self, datapath, port_no):
        """OVS port name for this port number ("" if unknown)."""
        try:
            port_objs = datapath.ports
        except AttributeError:
            return ""

        port = port_objs.get(port_no)
        if port is None:
            return ""

        name = getattr(port, "name", b"") or b""

        if isinstance(name, bytes):
            name = name.decode("utf-8", "ignore")

        return name

    def _is_tunnel_port(self, datapath, port_no):
        name = self._port_name(datapath, port_no)

        return bool(
            _VXLAN_TUNNEL_PORT_RE.match(name)
            or _VXLAN_ENDPOINT_PORT_RE.match(name)
            or _VXLAN_ROOT_VETH_RE.match(name)
        )

    # -- packet handling --------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):

        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        # Learn MAC locations for unicast forwarding from ANY non-link
        # source, including tunnel ports -- if a VTEP's MAC never gets
        # learned, traffic to it floods and can dodge the intended
        # underlay link entirely. host_ports is a separate concern: it
        # feeds edge-switch discovery only, and rightly still excludes
        # tunnel ports.
        if in_port not in self.switch_link_ports.get(dpid, set()):
            self.mac_to_dpid_port[eth.src] = (dpid, in_port)
            if not self._is_tunnel_port(datapath, in_port):
                self.host_ports.setdefault(dpid, set()).add(in_port)

        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            self._handle_arp(datapath, in_port, msg, pkt, eth)
            return

        if eth.ethertype == ether_types.ETH_TYPE_IP:
            self._handle_ipv4(datapath, in_port, msg, eth)
            return

    # -- proxy ARP --------------------------------------------------------

    def _handle_arp(self, datapath, in_port, msg, pkt, eth):

        arp_pkt = pkt.get_protocol(arp.arp)

        # Learn the sender's IP<->MAC, and keep it: execution_node /
        # RyuClient.get_ip_mac use this table to resolve VXLAN endpoint
        # MACs without asking Ryu's own host tracker.
        self.ip_to_mac[arp_pkt.src_ip] = eth.src

        if arp_pkt.opcode != arp.ARP_REQUEST:
            # An ARP reply (or anything else): forward it along the
            # learned MAC path if we know it, else flood.
            dst_loc = self.mac_to_dpid_port.get(eth.dst)

            if dst_loc is None:
                self._flood(datapath, in_port, msg)
                return

            dst_dpid, dst_port = dst_loc

            self._install_path_and_forward(
                datapath, in_port, msg, eth, dst_dpid, dst_port
            )
            return

        target_mac = self.ip_to_mac.get(arp_pkt.dst_ip)

        if target_mac is None:
            self._flood(datapath, in_port, msg)
            return

        self._send_arp_reply(datapath, in_port, eth, arp_pkt, target_mac)

    def _send_arp_reply(self, datapath, port, req_eth, req_arp, target_mac):

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        eth_reply = ethernet.ethernet(
            dst=req_eth.src,
            src=target_mac,
            ethertype=ether_types.ETH_TYPE_ARP
        )

        arp_reply = arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=target_mac,
            src_ip=req_arp.dst_ip,
            dst_mac=req_arp.src_mac,
            dst_ip=req_arp.src_ip
        )

        reply_pkt = packet.Packet()
        reply_pkt.add_protocol(eth_reply)
        reply_pkt.add_protocol(arp_reply)
        reply_pkt.serialize()

        actions = [parser.OFPActionOutput(port)]

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=reply_pkt.data
        )

        datapath.send_msg(out)

    # -- unicast IPv4 forwarding ------------------------------------------

    def _handle_ipv4(self, datapath, in_port, msg, eth):

        dst_loc = self.mac_to_dpid_port.get(eth.dst)

        if dst_loc is None:
            self._flood(datapath, in_port, msg)
            return

        dst_dpid, dst_port = dst_loc

        self._install_path_and_forward(
            datapath, in_port, msg, eth, dst_dpid, dst_port
        )

    def _install_path_and_forward(self, datapath, in_port, msg, eth,
                                   dst_dpid, dst_port):

        src_dpid = datapath.id

        if src_dpid == dst_dpid:
            path = [src_dpid]
        else:
            try:
                path = nx.shortest_path(self.net, src_dpid, dst_dpid)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                return

        for i, dpid in enumerate(path):

            if dpid == dst_dpid:
                out_port = dst_port
            else:
                out_port = self.net[dpid][path[i + 1]]["port"]

            dp = self.datapaths.get(dpid)

            if dp is None:
                continue

            parser = dp.ofproto_parser
            match = parser.OFPMatch(eth_dst=eth.dst)
            actions = [parser.OFPActionOutput(out_port)]

            self._add_flow(
                dp,
                FLOW_PRIORITY_FORWARD,
                match,
                actions,
                idle_timeout=FLOW_IDLE_TIMEOUT
            )

        first_out_port = (
            dst_port if src_dpid == dst_dpid
            else self.net[src_dpid][path[1]]["port"]
        )

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        actions = [parser.OFPActionOutput(first_out_port)]

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=(
                msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER
                else None
            )
        )

        datapath.send_msg(out)

    # -- loop-safe flooding ------------------------------------------------

    def _flood(self, datapath, in_port, msg):

        dpid = datapath.id
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Flood only to host ports and spanning-tree ports, never into
        # the overlay: tunnel ports are already excluded by
        # _is_tunnel_port, so unknown-destination traffic cannot leak
        # into the VXLAN plumbing.
        link_ports = self.switch_link_ports.get(dpid, set())

        try:
            port_objs = datapath.ports
        except AttributeError:
            port_objs = {}

        if port_objs:
            non_link_ports = set()
            for port_no, port in port_objs.items():
                if port_no in link_ports:
                    continue
                if self._is_tunnel_port(datapath, port_no):
                    continue
                non_link_ports.add(port_no)
        else:
            non_link_ports = set(self.host_ports.get(dpid, set()))

        out_ports = non_link_ports | self.tree_ports.get(dpid, set())
        out_ports.discard(in_port)
        out_ports.discard(ofproto.OFPP_LOCAL)
        out_ports.discard(ofproto.OFPP_CONTROLLER)

        if not out_ports:
            return

        actions = [parser.OFPActionOutput(p) for p in out_ports]

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=(
                msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER
                else None
            )
        )

        datapath.send_msg(out)

    # -- flow helper ------------------------------------------------------

    def _add_flow(self, datapath, priority, match, actions,
                  idle_timeout=0):

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(
            ofproto.OFPIT_APPLY_ACTIONS, actions
        )]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=inst,
            idle_timeout=idle_timeout
        )

        datapath.send_msg(mod)


# ----------------------------------------------------------------------
# REST API for the ip_to_mac table. 
# ----------------------------------------------------------------------

class TelemetryRestController(ControllerBase):

    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data[TELEMETRY_INSTANCE_NAME]

    @route("telemetry", "/telemetry/ip_mac_table", methods=["GET"])
    def get_ip_mac_table(self, req, **kwargs):

        body = json_lib.dumps(self.app.ip_to_mac).encode("utf-8")

        return Response(content_type="application/json", body=body)

    @route("telemetry", "/telemetry/ip_mac/{ip}", methods=["GET"])
    def get_ip_mac(self, req, ip, **kwargs):

        mac = self.app.ip_to_mac.get(ip)

        if mac is None:
            return Response(
                status=404, content_type="application/json",
                body=json_lib.dumps({"error": f"no MAC known for {ip}"}).encode("utf-8"),
            )

        return Response(
            content_type="application/json",
            body=json_lib.dumps({"ip": ip, "mac": mac}).encode("utf-8"),
        )

    @route("telemetry", "/telemetry/edge_switches", methods=["GET"])
    def get_edge_switches(self, req, **kwargs):

        edge_dpids = sorted(
            dpid for dpid, ports in self.app.host_ports.items() if ports
        )

        body = json_lib.dumps({"edge_switches": edge_dpids}).encode("utf-8")

        return Response(content_type="application/json", body=body)
