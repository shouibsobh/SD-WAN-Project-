RYU_IP = "127.0.0.1"
RYU_PORT = 8080

POLL_INTERVAL = 1

DATABASE = "database/telemetry.db"

# SLA measurement settings (ping + iperf3)
PING_COUNT = 5
IPERF_DURATION = 3

IPERF_BANDWIDTH = "10M"
# One iperf3 server per SOURCE switch, so two sources probing the same target don't collide on a port.
IPERF_BASE_PORT = 5201

SLA_POLL_INTERVAL = 15


BANDWIDTH_CHECK_INTERVAL_S = 60


BANDWIDTH_TUNNELS_PER_ROUND = 2

# SLA severity thresholds

SLA_LATENCY_WARNING_MS = 80
SLA_LATENCY_CRITICAL_MS = 150

SLA_JITTER_WARNING_MS = 20
SLA_JITTER_CRITICAL_MS = 50

SLA_LOSS_WARNING_PERCENT = 1
SLA_LOSS_CRITICAL_PERCENT = 5

SLA_MIN_BANDWIDTH_WARNING_MBPS = 5
SLA_MIN_BANDWIDTH_CRITICAL_MBPS = 2

# VXLAN overlay
TUNNEL_SUBNET_PREFIX = "10.200.0"
TUNNEL_VNI_BASE = 10000

# TunnelManager reconciliation interval
TUNNEL_POLL_INTERVAL = 2

# VXLAN UDP port
VXLAN_UDP_PORT = 4789

# Retries while an OVS port waits for a valid OpenFlow port number.
TUNNEL_PORT_RETRIES = 5
TUNNEL_PORT_RETRY_DELAY = 0.2

TUNNEL_NETNS_PREFIX = "ns-s"      # per-switch namespace, ns-s<dpid>
TUNNEL_ENDPOINT_SUFFIX = "-vx0"   # VTEP interface, s<dpid>-vx0