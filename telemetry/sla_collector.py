import json
import re
import subprocess
import threading
from wsgiref.simple_server import make_server, WSGIRequestHandler

from prometheus_client import Gauge, make_wsgi_app

from telemetry import config

# Separate port from link_metrics_app's 9200: that runs under
# ryu-manager in another process, so its registry isn't reachable here.
METRICS_PORT = 9201

# Labels name the tunnel, not a directed hop
_LABELS = ["switch_a", "switch_b"]

TUNNEL_LATENCY_GAUGE = Gauge(
    "tunnel_latency_ms", "Tunnel end-to-end RTT in ms", _LABELS
)
TUNNEL_JITTER_GAUGE = Gauge(
    "tunnel_jitter_ms", "Tunnel jitter (ping mdev) in ms", _LABELS
)
TUNNEL_LOSS_GAUGE = Gauge(
    "tunnel_packet_loss_percent", "Tunnel packet loss percent", _LABELS
)
TUNNEL_BANDWIDTH_GAUGE = Gauge(
    "tunnel_bandwidth_mbps", "Tunnel achieved bandwidth in Mbps", _LABELS
)
TUNNEL_UP_GAUGE = Gauge(
    "tunnel_up", "1 if the last probe got any reply, else 0", _LABELS
)

_metrics_server_started = False
_metrics_server_lock = threading.Lock()


# Silence per-scrape request logging
class _QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):
        pass


def start_metrics_server(port=METRICS_PORT):
    global _metrics_server_started

    with _metrics_server_lock:
        if _metrics_server_started:
            return

        try:
            httpd = make_server(
                "127.0.0.1", port, make_wsgi_app(),
                handler_class=_QuietHandler,
            )
        except OSError as exc:
            # Port taken (usually a stale process): drop exposition
            # rather than SLA measurement, which is the actual job.
            print(f"[sla_collector] cannot expose /metrics on :{port}: {exc}")
            return

        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        _metrics_server_started = True

        print(f"[sla_collector] /metrics exposed on :{port}")

# Per-packet wait (-W), not the probe's total duration. Kept at 1s
# because an unreachable tunnel -- the case that most needs fast
# detection -- otherwise costs PING_COUNT * timeout before reporting.
PING_TIMEOUT_S = 1

_PING_SUMMARY_RE = re.compile(
    r"(\d+) packets transmitted, (\d+)(?: packets)? received"
)
_PING_RTT_RE = re.compile(
    r"= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms"
)


class SlaCollector:
   

    def __init__(self, export_to_prometheus=False):
        # (dpid, port) pairs with a confirmed iperf3 server, so the
        # socket check happens once per target, not every round.
        self._servers_started = set()

        self._bandwidth_lock = threading.Lock()

        if export_to_prometheus:
            start_metrics_server()

    @staticmethod
    def _netns_name(dpid):
        # Must match TunnelManager._netns_name.
        return f"ns-s{dpid}"

    @staticmethod
    def _tunnel_ip(dpid):
        # Must match TunnelManager._tunnel_ip. Lives on br-vx in ns-s<dpid>.
        return f"{config.TUNNEL_SUBNET_PREFIX}.{dpid}"

    @staticmethod
    def _netns_wrap(netns, cmd):
        return ["sudo", "ip", "netns", "exec", netns, *cmd]

    # ------------------------------------------------------------
    # ping: latency + jitter (mdev) + packet loss
    # ------------------------------------------------------------

    def _ping(self, source_dpid, source_iface, target_ip, count=None):

        count = count or min(config.PING_COUNT, 5)
        netns = self._netns_name(source_dpid)

        cmd = self._netns_wrap(netns, [
            "ping", "-I", source_iface,
            "-c", str(count),
            "-W", str(PING_TIMEOUT_S),
            target_ip,
        ])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=count * PING_TIMEOUT_S + 5,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"[sla_collector] ping {target_ip} via {netns}/{source_iface} "
                  f"failed to run: {exc}")
            return None

        output = result.stdout

        summary = _PING_SUMMARY_RE.search(output)
        if summary is None:
            # Unreachable is 100% loss, not "no data" -- the caller has
            # to tell that apart from a cold start.
            return {
                "latency_ms": None,
                "jitter_ms": None,
                "packet_loss_percent": 100.0,
            }

        sent, received = int(summary.group(1)), int(summary.group(2))
        loss_percent = (
            ((sent - received) / sent) * 100 if sent > 0 else 100.0
        )

        rtt = _PING_RTT_RE.search(output)
        if rtt is None or received == 0:
            return {
                "latency_ms": None,
                "jitter_ms": None,
                "packet_loss_percent": loss_percent,
            }

        _min, avg, _max, mdev = (float(x) for x in rtt.groups())

        return {
            "latency_ms": avg,
            "jitter_ms": mdev,   # mdev is ping's own jitter estimate
            "packet_loss_percent": loss_percent,
        }


    def _iperf_port(self, source_dpid):
        return config.IPERF_BASE_PORT + int(source_dpid)

    def _ensure_server(self, target_dpid, port):
        """Start `iperf3 -s` in the target's namespace, bound to its VTEP.

        Idempotent, and the socket check is skipped once confirmed.
        """
        cache_key = (int(target_dpid), port)

        if cache_key in self._servers_started:
            return True

        netns = self._netns_name(target_dpid)
        bind_ip = self._tunnel_ip(target_dpid)

        # Adopt an existing server (left over from an earlier run)
        # rather than starting a duplicate. The timeout matters: this
        # runs on the poll thread, so a wedged `ip netns exec` would
        # stall SLA monitoring for every tunnel, not just this one.
        try:
            check = subprocess.run(
                self._netns_wrap(netns, ["ss", "-lnt", f"sport = :{port}"]),
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"[sla_collector] port check in {netns} failed: {exc}")
            return False

        if "LISTEN" in check.stdout:
            self._servers_started.add(cache_key)
            return True

        # -D daemonizes so the server outlives a probe round. 
        try:
            result = subprocess.run(
                self._netns_wrap(netns, [
                    "iperf3", "-s", "-B", bind_ip, "-p", str(port), "-D",
                ]),
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"[sla_collector] starting iperf3 server in {netns} "
                  f"failed: {exc}")
            return False

        if result.returncode != 0:
            print(f"[sla_collector] could not start iperf3 server in "
                  f"{netns} on {bind_ip}:{port}: {result.stderr.strip()}")
            return False

        self._servers_started.add(cache_key)
        return True

    def _iperf(self, source_dpid, target_dpid, target_ip, duration=None):

        duration = duration or config.IPERF_DURATION
        netns = self._netns_name(source_dpid)
        port = self._iperf_port(source_dpid)

        if not self._ensure_server(target_dpid, port):
            return None

        source_ip = self._tunnel_ip(source_dpid)

        cmd = self._netns_wrap(netns, [
            "iperf3", "-c", target_ip,
            "-B", source_ip,
            "-p", str(port),
            "-u", "-b", config.IPERF_BANDWIDTH,
            "-t", str(duration),
            "-J",
        ])

        # Serialized so probes measure the network, not each other.
        with self._bandwidth_lock:
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=duration + 10,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                print(f"[sla_collector] iperf3 {target_ip} via {netns} "
                      f"failed to run: {exc}")
                return None

        try:
            report = json.loads(result.stdout)
        except ValueError:
            # Non-JSON means iperf3 died before reporting (bad flag,
            # server vanished); stderr is the useful part.
            print(f"[sla_collector] iperf3 {target_ip}: unparseable output "
                  f"({result.stderr.strip() or 'no stderr'})")
            return None

        if "error" in report:
            print(f"[sla_collector] iperf3 {target_ip}: {report['error']}")
            return None

        try:
            summary = report["end"]["sum"]
            offered_mbps = summary["bits_per_second"] / 1e6
            lost_percent = summary.get("lost_percent", 0.0)
        except (KeyError, TypeError):
            return None

        return max(offered_mbps * (1.0 - (lost_percent / 100.0)), 0.0)

    # ------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------

    def measure_tunnel(self, switch_a, switch_b, remote_ip_a, remote_ip_b,
                        with_bandwidth=False):
        """Probe from switch_a's VTEP to switch_b's.

        Returns {latency_ms, jitter_ms, packet_loss_percent,
        bandwidth_Mbps}; any field is None if that probe failed or
        wasn't requested.
        """
        source_iface = "br-vx"

        ping_result = self._ping(switch_a, source_iface, remote_ip_b) or {
            "latency_ms": None,
            "jitter_ms": None,
            "packet_loss_percent": None,
        }

        bandwidth_mbps = None
        if with_bandwidth:
            bandwidth_mbps = self._iperf(switch_a, switch_b, remote_ip_b)

        metrics = {
            "latency_ms": ping_result["latency_ms"],
            "jitter_ms": ping_result["jitter_ms"],
            "packet_loss_percent": ping_result["packet_loss_percent"],
            "bandwidth_Mbps": bandwidth_mbps,
        }

        self._publish(switch_a, switch_b, metrics)

        return metrics

    # ------------------------------------------------------------
    # Prometheus exposition
    # ------------------------------------------------------------

    def _publish(self, switch_a, switch_b, metrics):
        #Push this round's measurements into the gauges.
        
        labels = {"switch_a": str(switch_a), "switch_b": str(switch_b)}

        gauges = (
            (TUNNEL_LATENCY_GAUGE, metrics["latency_ms"]),
            (TUNNEL_JITTER_GAUGE, metrics["jitter_ms"]),
            (TUNNEL_LOSS_GAUGE, metrics["packet_loss_percent"]),
            (TUNNEL_BANDWIDTH_GAUGE, metrics["bandwidth_Mbps"]),
        )

        for gauge, value in gauges:
            if value is not None:
                gauge.labels(**labels).set(value)

        loss = metrics["packet_loss_percent"]
        reachable = metrics["latency_ms"] is not None or (
            loss is not None and loss < 100.0
        )
        TUNNEL_UP_GAUGE.labels(**labels).set(1.0 if reachable else 0.0)