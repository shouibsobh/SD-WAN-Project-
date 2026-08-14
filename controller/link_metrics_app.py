"""Per-link underlay metric 

Per directed switch-to-switch link:

    latency_ms   timestamped probe frames shaped as LLDP -- reusing
                 ryu.lib.packet.lldp, still distinguishable from the
                 discovery frames ryu.topology.switches sends
    jitter_ms    mean |latency[i] - latency[i-1]| over JITTER_WINDOW
    utilization  OFPPortStats deltas over the port's curr_speed
    packet_loss  source tx_packets vs destination rx_packets, same interval


"""

import struct
import time
from collections import defaultdict, deque

from prometheus_client import Gauge, make_wsgi_app
from wsgiref.simple_server import make_server, WSGIRequestHandler
import threading


from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    MAIN_DISPATCHER,
    DEAD_DISPATCHER,
    set_ev_cls,
)
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ether_types, lldp
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event
from ryu.topology.api import get_link


PROBE_INTERVAL_S = 1
STATS_INTERVAL_S = 1
STATS_SETTLE_S = 0.3    # let every switch's OFPPortStatsReply land before
                        # reconciling. Per-reply reconciliation pairs a
                        # fresh reading with a stale one and reads as
                        # phantom loss -- links flapped
JITTER_WINDOW = 10

MIN_TX_DELTA_FOR_LOSS = 5   # packets. Below this a 1-packet skew between
                            # the ends swings loss wildly: 5 tx vs 4 rx
                            # reads as 20% from pure timing noise.

METRICS_PORT = 9200   # Prometheus scrapes http://<controller-host>:9200/metrics

# Marks our probe frames so we can ignore anything that isn't ours
# (including ryu.topology.switches' own LLDP discovery frames, and any
# stray real LLDP traffic on the wire).
CHASSIS_PREFIX = b"netagent-probe:"
PROBE_OUI = b"\x00\x1a\x2b"        # arbitrary project-local OUI
PROBE_SUBTYPE = 1

LATENCY_GAUGE = Gauge(
    "link_latency_ms", "Link latency in ms", ["src_dpid", "dst_dpid"]
)
JITTER_GAUGE = Gauge(
    "link_jitter_ms", "Link jitter in ms", ["src_dpid", "dst_dpid"]
)
UTILIZATION_GAUGE = Gauge(
    "link_utilization_percent", "Link utilization percent", ["src_dpid", "dst_dpid"]
)
PACKET_LOSS_GAUGE = Gauge(
    "link_packet_loss_percent", "Link packet loss percent", ["src_dpid", "dst_dpid"]
)

def start_prometheus_server(port):
    app = make_wsgi_app()

 
    class _QuietHandler(WSGIRequestHandler):
        def log_message(self, fmt, *args):
            pass

    httpd = make_server(
        "127.0.0.1",
        port,
        app,
        handler_class=_QuietHandler,
    )

    httpd.serve_forever()

class LinkMetricsApp(app_manager.RyuApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):

        super().__init__(*args, **kwargs)

        threading.Thread(
            target=start_prometheus_server,
            args=(METRICS_PORT,),
            daemon=True
        ).start()

        self.datapaths = {}

        # (src_dpid, dst_dpid) -> (src_port, dst_port)
        self.link_ports = {}

        # (src_dpid, dst_dpid) -> deque of recent latency samples (ms)
        self._latency_samples = defaultdict(
            lambda: deque(maxlen=JITTER_WINDOW)
        )

        # (dpid, port_no) -> curr_speed in kbps
        self._port_speed = {}

        # (dpid, port_no) -> {"time","rx_packets","tx_packets"}
        self._prev_port_stats = {}

        # (dpid, port_no) -> latest computed delta since last poll:
        # {"tx_packets_delta":..., "rx_packets_delta":..., "utilization":...}
        self._latest_side = {}

        self.logger.info("[link_metrics] /metrics exposed on :%d", METRICS_PORT)

        self._probe_thread = hub.spawn(self._probe_loop)
        self._stats_thread = hub.spawn(self._stats_loop)

    # ------------------------------------------------------------
    # datapath / topology bookkeeping
    # ------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):

        dp = ev.datapath

        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            dp.send_msg(dp.ofproto_parser.OFPPortDescStatsRequest(dp, 0))
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def _port_desc_reply(self, ev):

        dp = ev.msg.datapath

        for port in ev.msg.body:
            if port.port_no >= dp.ofproto.OFPP_MAX:
                continue
            self._port_speed[(dp.id, port.port_no)] = port.curr_speed  # kbps

    @set_ev_cls(event.EventLinkAdd)
    def _link_add(self, ev):
        self._refresh_links()

    @set_ev_cls(event.EventLinkDelete)
    def _link_delete(self, ev):
        self._refresh_links()

    def _refresh_links(self):

        self.link_ports = {
            (link.src.dpid, link.dst.dpid): (link.src.port_no, link.dst.port_no)
            for link in get_link(self, None)
        }

    # ------------------------------------------------------------
    # Latency + jitter: LLDP-shaped probes with an embedded timestamp
    # ------------------------------------------------------------

    def _build_probe(self, src_dpid):

        send_ts = time.time()

        tlvs = [
            lldp.ChassisID(
                subtype=lldp.ChassisID.SUB_LOCALLY_ASSIGNED,
                chassis_id=CHASSIS_PREFIX + str(src_dpid).encode(),
            ),
            lldp.PortID(
                subtype=lldp.PortID.SUB_LOCALLY_ASSIGNED,
                port_id=b"probe",
            ),
            lldp.TTL(ttl=5),
            lldp.OrganizationallySpecific(
                oui=PROBE_OUI,
                subtype=PROBE_SUBTYPE,
                info=struct.pack("!d", send_ts),
            ),
            lldp.End(),
        ]

        eth = ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_LLDP,
            src="00:00:00:00:00:01",
            dst=lldp.LLDP_MAC_NEAREST_BRIDGE,
        )

        pkt = packet.Packet()
        pkt.add_protocol(eth)
        pkt.add_protocol(lldp.lldp(tlvs))
        pkt.serialize()

        return pkt.data

    def _probe_loop(self):

        while True:

            for (src_dpid, _dst_dpid), (src_port, _dst_port) in self.link_ports.items():

                dp = self.datapaths.get(src_dpid)
                if dp is None:
                    continue

                ofproto = dp.ofproto
                parser = dp.ofproto_parser

                data = self._build_probe(src_dpid)

                dp.send_msg(parser.OFPPacketOut(
                    datapath=dp,
                    buffer_id=ofproto.OFP_NO_BUFFER,
                    in_port=ofproto.OFPP_CONTROLLER,
                    actions=[parser.OFPActionOutput(src_port)],
                    data=data,
                ))

            hub.sleep(PROBE_INTERVAL_S)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):

        dst_dpid = ev.msg.datapath.id
        pkt = packet.Packet(ev.msg.data)

        eth = pkt.get_protocols(ethernet.ethernet)
        if not eth or eth[0].ethertype != ether_types.ETH_TYPE_LLDP:
            return  # not our concern -- let other apps handle it

        try:
            lldp_pkt = pkt.get_protocol(lldp.lldp)
        except Exception:
            return

        if lldp_pkt is None:
            return

        src_dpid = None
        send_ts = None

        for tlv in lldp_pkt.tlvs:

            if isinstance(tlv, lldp.ChassisID) and tlv.chassis_id.startswith(CHASSIS_PREFIX):
                try:
                    src_dpid = int(tlv.chassis_id[len(CHASSIS_PREFIX):].decode())
                except ValueError:
                    return

            elif (isinstance(tlv, lldp.OrganizationallySpecific)
                    and tlv.oui == PROBE_OUI and tlv.subtype == PROBE_SUBTYPE):
                send_ts = struct.unpack("!d", tlv.info)[0]

        if src_dpid is None or send_ts is None:
            return  # real LLDP frame or someone else's -- ignore quietly

        latency_ms = max((time.time() - send_ts) * 1000.0, 0.0)
        self._record_latency(src_dpid, dst_dpid, latency_ms)

    def _record_latency(self, src_dpid, dst_dpid, latency_ms):

        key = (src_dpid, dst_dpid)
        samples = self._latency_samples[key]
        samples.append(latency_ms)

        jitter_ms = 0.0
        if len(samples) > 1:
            diffs = [abs(samples[i] - samples[i - 1]) for i in range(1, len(samples))]
            jitter_ms = sum(diffs) / len(diffs)

        labels = {"src_dpid": str(src_dpid), "dst_dpid": str(dst_dpid)}
        LATENCY_GAUGE.labels(**labels).set(latency_ms)
        JITTER_GAUGE.labels(**labels).set(jitter_ms)

    # ------------------------------------------------------------
    # Utilization + packet loss: OFPPortStatsRequest/Reply deltas
    # ------------------------------------------------------------

    def _stats_loop(self):

        while True:

            for dp in list(self.datapaths.values()):
                parser = dp.ofproto_parser
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))

            # let every switch's reply land before comparing both ends
            # of a link -- see STATS_SETTLE_S's comment above
            hub.sleep(STATS_SETTLE_S)
            self._reconcile_links()

            hub.sleep(max(STATS_INTERVAL_S - STATS_SETTLE_S, 0))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply(self, ev):

        dp = ev.msg.datapath
        now = time.time()

        for stat in ev.msg.body:

            if stat.port_no >= dp.ofproto.OFPP_MAX:
                continue

            key = (dp.id, stat.port_no)
            prev = self._prev_port_stats.get(key)

            self._prev_port_stats[key] = {
                "time": now,
                "rx_packets": stat.rx_packets,
                "tx_packets": stat.tx_packets,
                "tx_bytes": stat.tx_bytes,
            }

            if prev is None:
                continue

            dt = now - prev["time"]
            if dt <= 0:
                continue

            tx_bps = (stat.tx_bytes - prev["tx_bytes"]) * 8 / dt
            speed_kbps = self._port_speed.get(key)
            utilization = (
                min((tx_bps / 1000) / speed_kbps * 100, 100)
                if speed_kbps else None
            )

            self._latest_side[key] = {
                "tx_packets_delta": stat.tx_packets - prev["tx_packets"],
                "rx_packets_delta": stat.rx_packets - prev["rx_packets"],
                "utilization_percent": utilization,
            }

    def _reconcile_links(self):
        """Fold both ends of each link (src tx delta, dst rx delta) into
        one utilization + packet_loss figure.

        Runs after every port-stats batch, so each link uses the freshest
        side data available.
        """

        for (src_dpid, dst_dpid), (src_port, dst_port) in self.link_ports.items():

            src_side = self._latest_side.get((src_dpid, src_port))
            dst_side = self._latest_side.get((dst_dpid, dst_port))

            if src_side is None:
                continue

            tx_delta = src_side["tx_packets_delta"]
            packet_loss_percent = None

            if dst_side is not None:
                if tx_delta >= MIN_TX_DELTA_FOR_LOSS:
                    rx_delta = dst_side["rx_packets_delta"]
                    lost = max(tx_delta - rx_delta, 0)
                    packet_loss_percent = (lost / tx_delta) * 100
                else:
                    packet_loss_percent = 0.0

            labels = {"src_dpid": str(src_dpid), "dst_dpid": str(dst_dpid)}
            utilization_percent = src_side["utilization_percent"]

            if utilization_percent is not None:
                UTILIZATION_GAUGE.labels(**labels).set(utilization_percent)

            if packet_loss_percent is not None:
                PACKET_LOSS_GAUGE.labels(**labels).set(packet_loss_percent)