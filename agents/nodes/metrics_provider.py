"""Prometheus link gauges -> PathScorer's feature vector.

link_metrics_app.py publishes utilization / latency / jitter / loss per
link; this turns them into the fixed 4-slot vector path_scorer.py
scores. Order matters -- the weight vector is positional:

    [0] max utilization %      over the path's hops
    [1] total latency / 500    summed over hops
    [2] max jitter / 100
    [3] max packet loss %
"""

import requests

PROMETHEUS_URL = "http://localhost:9090"
PROMETHEUS_TIMEOUT_S = 3


ANOMALY_STATES = ("WARNING", "CRITICAL", "LINK_ERRORS")

# PathScorer is a linear model trained by unclamped SGD, so an unbounded
# feature (summed latency over many hops) could blow up its weight and
# diverge. Normalising to roughly [0, 1] bounds each step.
UTIL_NORM = 100.0        # already a percentage
LATENCY_NORM_MS = 500.0  # paths shouldn't normally exceed this
JITTER_NORM_MS = 100.0
LOSS_NORM = 100.0        # already a percentage


def _clip(value, lo=-1.0, hi=1.0):
    return max(lo, min(hi, value))


def _query_instant(metric_name, label_matchers):
    matcher_str = ",".join(f'{k}="{v}"' for k, v in label_matchers.items())

    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": f"{metric_name}{{{matcher_str}}}"},
            timeout=PROMETHEUS_TIMEOUT_S,
        )
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
        return float(result[0]["value"][1]) if result else None

    except (requests.exceptions.RequestException, KeyError, ValueError, IndexError):
        return None


def get_link_metrics(src_dpid, dst_dpid):
    """The four raw gauges for one directed link, None where absent."""
    labels = {"src_dpid": str(src_dpid), "dst_dpid": str(dst_dpid)}

    return {
        "utilization_percent": _query_instant("link_utilization_percent", labels),
        "latency_ms": _query_instant("link_latency_ms", labels),
        "jitter_ms": _query_instant("link_jitter_ms", labels),
        "packet_loss_percent": _query_instant("link_packet_loss_percent", labels),
    }


def _link_metrics(src_dpid, dst_dpid):
    """Same, with None coerced to 0.0 so max()/sum() can't blow up."""
    raw = get_link_metrics(src_dpid, dst_dpid)
    return {k: (v if v is not None else 0.0) for k, v in raw.items()}


def aggregate_path_metrics(path, topology):
    """Collapse a path's hops into one metrics dict, worst hop wins.

    Latency is summed (cumulative delay); the rest take the worst hop.
    """
    hops = [
        (u, v) for u, v in zip(path, path[1:])
        if u.startswith("s") and v.startswith("s")
    ]

    max_util = total_latency = max_jitter = max_loss = 0.0

    for u, v in hops:
        data = topology.graph.get_edge_data(u, v) or {}
        src_dpid, dst_dpid = data.get("src_switch"), data.get("dst_switch")

        if src_dpid is None or dst_dpid is None:
            continue

        m = _link_metrics(src_dpid, dst_dpid)

        max_util = max(max_util, m["utilization_percent"])
        total_latency += m["latency_ms"]
        max_jitter = max(max_jitter, m["jitter_ms"])
        max_loss = max(max_loss, m["packet_loss_percent"])

    return {
        "utilization_percent": max_util,
        "latency_ms": total_latency,
        "jitter_ms": max_jitter,
        "packet_loss_percent": max_loss,
    }


def features_from_raw(utilization_percent, latency_ms, jitter_ms, packet_loss_percent):
    """The one place raw metrics become a feature vector.

    warmup.py imports this too, so offline-trained weights land on the
    same scale as ones trained online from Prometheus.
    """
    return [
        _clip(utilization_percent / UTIL_NORM, 0.0, 1.0),
        _clip(latency_ms / LATENCY_NORM_MS, 0.0, 1.0),
        _clip(jitter_ms / JITTER_NORM_MS, 0.0, 1.0),
        _clip(packet_loss_percent / LOSS_NORM, 0.0, 1.0),
    ]


def get_path_features(path, topology):
    """Feature vector for a path, e.g. ["s1", "s3", "s2"]."""
    agg = aggregate_path_metrics(path, topology)

    return features_from_raw(
        agg["utilization_percent"], agg["latency_ms"],
        agg["jitter_ms"], agg["packet_loss_percent"],
    )


# ----------------------------------------------------------------
# Tunnel SLA -- the outcome side, not the feature side
# ----------------------------------------------------------------

TUNNEL_SLA_KEYS = ("latency_ms", "jitter_ms", "packet_loss_percent")


def get_tunnel_sla(switch_a, switch_b):

    for a, b in ((switch_a, switch_b), (switch_b, switch_a)):
        labels = {"switch_a": str(a), "switch_b": str(b)}

        sla = {
            "latency_ms": _query_instant("tunnel_latency_ms", labels),
            "jitter_ms": _query_instant("tunnel_jitter_ms", labels),
            "packet_loss_percent": _query_instant(
                "tunnel_packet_loss_percent", labels
            ),
        }

        if any(v is not None for v in sla.values()):
            return sla

    return {k: None for k in TUNNEL_SLA_KEYS}


def tunnel_sla_vector(sla):
    """Normalise an SLA dict onto the same [0,1] scale as the features.

    None stays None: a missing probe is not a healthy one, and the caller
    has to be able to tell them apart.
    """
    norms = (
        ("latency_ms", LATENCY_NORM_MS),
        ("jitter_ms", JITTER_NORM_MS),
        ("packet_loss_percent", LOSS_NORM),
    )

    return [
        None if sla.get(key) is None
        else _clip(sla[key] / norm, 0.0, 1.0)
        for key, norm in norms
    ]
