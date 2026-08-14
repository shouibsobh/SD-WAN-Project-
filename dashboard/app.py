"""Streamlit dashboard for the self-healing SD-WAN project.

Reads from what the project already produces; collects nothing itself:

  Prometheus :9090   link_*   per-hop     (controller/link_metrics_app.py)
                     tunnel_* end-to-end  (telemetry/sla_collector.py)
  SQLite             network_events, agent_decisions, chaos_log, sla_status
  Ryu REST           switch/link discovery via TopologyManager
  tc netem           fault injection via chaos/fault_injector.py

"""

import json
import math
import os
import sys
import threading
import time

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from chaos.fault_injector import FaultInjector
from telemetry import config
from telemetry.database import DatabaseManager
from telemetry.topology_manager import TopologyManager

PROMETHEUS_URL = "http://localhost:9090"

# What telemetry_agent persisted (determine_sla_state + debouncer) --
# i.e. exactly the states the agent acts on.
STATE_COLOR = {
    "NORMAL": "#2ecc71",
    "WARNING": "#f39c12",
    "CRITICAL": "#e74c3c",
    "UNREACHABLE": "#8e44ad",
    "UNKNOWN": "#95a5a6",
}

SEVERITY_STYLE = {
    "INFO": "background-color: #d5f5e3",
    "WARNING": "background-color: #fdebd0",
    "CRITICAL": "background-color: #fadbd8",
}

FAULT_FILL = "rgba(231, 76, 60, 0.13)"

st.set_page_config(
    page_title="SD-WAN Control Plane",
    page_icon="🛰️",
    layout="wide",
)


# Cached singletons, one per session. The DB connection is opened with
# check_same_thread=False and discover() rebuilds its graph each call.
@st.cache_resource
def get_db():
    return DatabaseManager()


@st.cache_resource
def get_topology():
    return TopologyManager()


@st.cache_resource
def get_injector():
    return FaultInjector()


db = get_db()
topology = get_topology()


# ======================================================================
# Prometheus access
# ======================================================================

def _label_of(metric, label_keys, joiner):
    return joiner.join(str(metric.get(key, "?")) for key in label_keys)


@st.cache_data(ttl=8, show_spinner=False)
def prom_range(query, minutes, label_keys, joiner=" <-> ", step=15):
    end = int(time.time())
    start = end - int(minutes * 60)

    try:
        response = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "step": step},
            timeout=6,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return pd.DataFrame(), str(exc)

    if payload.get("status") != "success":
        return pd.DataFrame(), payload.get("error", "query rejected")

    stamps, labels, values = [], [], []

    for series in payload["data"]["result"]:
        label = _label_of(series["metric"], label_keys, joiner)
        for stamp, value in series["values"]:
            stamps.append(float(stamp))
            labels.append(label)
            values.append(float(value))

    frame = pd.DataFrame({
        "time": pd.to_datetime(stamps, unit="s"),
        "series": labels,
        "value": values,
    })

    return frame, None


@st.cache_data(ttl=8, show_spinner=False)
def prom_latest(query, label_keys):
    """Instant query -> {label tuple: float}. Empty dict if unavailable."""
    try:
        response = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=6,
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        return {}

    if payload.get("status") != "success":
        return {}

    return {
        tuple(str(s["metric"].get(k, "")) for k in label_keys): float(s["value"][1])
        for s in payload["data"]["result"]
    }


def prometheus_alive():
    try:
        return requests.get(f"{PROMETHEUS_URL}/-/healthy", timeout=3).ok
    except requests.RequestException:
        return False


# ======================================================================
# Fault windows -- reconstructed from chaos_log for chart annotation
# ======================================================================

def _window(pair, start_row, end):
    return {
        "pair": pair,
        "label": f"s{pair[0]} <-> s{pair[1]}",
        "start": start_row["timestamp"],
        "end": end,
        "fault_type": start_row["fault_type"],
        "parameters": start_row["parameters"],
    }


def fault_windows(chaos_rows, horizon_start):

    rows = sorted(chaos_rows, key=lambda r: r["timestamp"])
    open_faults = {}
    windows = []

    for row in rows:
        pair = (row["switch_a"], row["switch_b"])

        if row["result"] == "injected":
            # Back-to-back injections (raising the impairment without
            # clearing) are one outage; keep the earliest start.
            open_faults.setdefault(pair, row)

        elif row["result"] == "cleared" and pair in open_faults:
            windows.append(_window(pair, open_faults.pop(pair), row["timestamp"]))

    for pair, start_row in open_faults.items():
        windows.append(_window(pair, start_row, None))

    # Keep anything overlapping the visible horizon, including a window
    # that started before it and is still running.
    return [
        w for w in windows
        if w["end"] is None or w["end"] >= horizon_start
    ]


def describe_fault(window):
    """Compact human label for a fault window, e.g. 'delay 100ms, loss 5%'."""
    try:
        params = json.loads(window["parameters"] or "{}")
    except (ValueError, TypeError):
        params = {}

    if window["fault_type"] == "link_state":
        return "link down"

    parts = []
    if params.get("delay_ms"):
        parts.append(f"delay {params['delay_ms']}ms")
    if params.get("jitter_ms"):
        parts.append(f"jitter {params['jitter_ms']}ms")
    if params.get("loss_percent"):
        parts.append(f"loss {params['loss_percent']}%")
    if params.get("rate_mbit"):
        parts.append(f"rate {params['rate_mbit']}Mbit")

    return ", ".join(parts) if parts else window["fault_type"]


def fault_shapes(windows, now):
    #Fault windows as (shapes, annotations) dicts for a chart layout.
    shapes, annotations = [], []

    for window in windows:
        start = pd.to_datetime(window["start"], unit="s")
        end = pd.to_datetime(window["end"] if window["end"] else now, unit="s")
        suffix = "" if window["end"] else " (active)"

        shapes.append({
            "type": "rect",
            "xref": "x", "yref": "paper",
            "x0": start, "x1": end, "y0": 0, "y1": 1,
            "fillcolor": FAULT_FILL,
            "line": {"width": 0},
            "layer": "below",
        })
        annotations.append({
            "x": start, "y": 1.0,
            "xref": "x", "yref": "paper",
            "text": f"{window['label']}: {describe_fault(window)}{suffix}",
            "showarrow": False,
            "xanchor": "left", "yanchor": "bottom",
            "font": {"size": 10, "color": "#c0392b"},
        })

    return shapes, annotations


# ======================================================================
# Deterministic topology layout
# ======================================================================

def fixed_positions(graph, ring_slots_min=8):
    #Map every node to a position that depends only on its identity.
    switch_ids = []
    for node, data in graph.nodes(data=True):
        if data.get("type") == "switch" and node[1:].isdigit():
            switch_ids.append(int(node[1:]))

    slots = max(ring_slots_min, max(switch_ids) if switch_ids else ring_slots_min)

    pos = {}
    for dpid in switch_ids:
        angle = 2.0 * math.pi * ((dpid - 1) % slots) / slots
        pos[f"s{dpid}"] = (math.cos(angle), math.sin(angle))

    # Hosts (and anything else non-switch) hang just outside the ring,
    # on the same bearing as the switch they attach to.
    for node, data in graph.nodes(data=True):
        if node in pos:
            continue

        anchors = [n for n in graph.neighbors(node) if n in pos]
        if anchors:
            ax, ay = pos[anchors[0]]
            norm = math.hypot(ax, ay) or 1.0
            offset = 0.42 + 0.10 * (sorted(graph.neighbors(anchors[0])).index(node)
                                    if node in graph.neighbors(anchors[0]) else 0)
            pos[node] = (ax + ax / norm * offset, ay + ay / norm * offset)
        else:
            pos[node] = (0.0, 0.0)

    return pos


# ======================================================================
# Sidebar
# ======================================================================

st.sidebar.title("SD-WAN Control Plane")

window_minutes = st.sidebar.select_slider(
    "Chart time window",
    options=[5, 15, 30, 60, 180, 360],
    value=30,
    format_func=lambda m: f"{m} min" if m < 60 else f"{m // 60} h",
)


auto_refresh = st.sidebar.checkbox("Auto refresh", value=False)
refresh_interval = st.sidebar.slider("Refresh interval (s)", 5, 60, 15)

if st.sidebar.button("Refresh now", width="stretch"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.divider()
st.sidebar.subheader("Data sources")

prom_up = prometheus_alive()
link_series = prom_latest("link_latency_ms", ("src_dpid", "dst_dpid"))
tunnel_series = prom_latest("tunnel_up", ("switch_a", "switch_b"))

st.sidebar.write(f"{'🟢' if prom_up else '🔴'} Prometheus :9090")
st.sidebar.write(
    f"{'🟢' if link_series else '🔴'} Link exporter :9200 "
    f"({len(link_series)} series)"
)
st.sidebar.write(
    f"{'🟢' if tunnel_series else '🔴'} SLA exporter :9201 "
    f"({len(tunnel_series)} series)"
)

try:
    topology.discover()
    topo_ok = topology.graph.number_of_nodes() > 0
    topo_error = None
except Exception as exc:
    topo_ok = False
    topo_error = str(exc)

st.sidebar.write(f"{'🟢' if topo_ok else '🔴'} Ryu controller")
if topo_error:
    st.sidebar.caption(f"Ryu: {topo_error}")

st.sidebar.divider()
st.sidebar.caption(
    "Telemetry + LangGraph agents + chaos engineering. "
    "Pure Python, no separate front-end."
)


# ======================================================================
# Shared state for the whole page
# ======================================================================

now = time.time()
horizon_start = now - window_minutes * 60

chaos_rows = db.get_recent_chaos_events(limit=500)
windows = fault_windows(chaos_rows, horizon_start)
active_faults = [w for w in windows if w["end"] is None]

sla_rows = {}
for src, dst, state, changed in db.connection.execute(
    "SELECT src_host, dst_host, state, last_change FROM sla_status"
):
    try:
        pair = (int(src.split("s")[-1]), int(dst.split("s")[-1]))
    except ValueError:
        continue
    sla_rows[tuple(sorted(pair))] = {"state": state, "last_change": changed}

st.title("🛰️ Self-Healing SD-WAN")

if active_faults:
    st.error(
        "**Active fault"
        f"{'s' if len(active_faults) > 1 else ''}:** "
        + " · ".join(
            f"{w['label']} — {describe_fault(w)} "
            f"(for {int(now - w['start'])}s)"
            for w in active_faults
        )
    )


# ======================================================================
# Section 1 -- Overview
# ======================================================================

st.header("Network Overview")

switch_count = sum(
    1 for _, d in topology.graph.nodes(data=True) if d.get("type") == "switch"
)
host_count = topology.graph.number_of_nodes() - switch_count
link_count = topology.graph.number_of_edges()

state_counts = {"NORMAL": 0, "WARNING": 0, "CRITICAL": 0, "UNREACHABLE": 0}
for entry in sla_rows.values():
    if entry["state"] in state_counts:
        state_counts[entry["state"]] += 1

tunnels_up = sum(1 for v in tunnel_series.values() if v >= 1.0)
tunnels_total = len(tunnel_series)

# One dict per metric, shared by the summary and the per-tunnel table.
latest_latency = prom_latest("tunnel_latency_ms", ("switch_a", "switch_b"))
latest_loss = prom_latest("tunnel_packet_loss_percent", ("switch_a", "switch_b"))
latest_bandwidth = prom_latest("tunnel_bandwidth_mbps", ("switch_a", "switch_b"))

latencies = list(latest_latency.values())
losses = list(latest_loss.values())
bandwidths = list(latest_bandwidth.values())

row = st.columns(6)
row[0].metric("Switches", switch_count)
row[1].metric("Hosts", host_count)
row[2].metric("Physical links", link_count)
row[3].metric(
    "Tunnels up",
    f"{tunnels_up}/{tunnels_total}" if tunnels_total else "n/a",
    delta=None if tunnels_up == tunnels_total else f"-{tunnels_total - tunnels_up}",
    delta_color="inverse",
)
row[4].metric("Events (1 h)", db.count_events(since_seconds=3600))
row[5].metric("Active faults", len(active_faults))

row = st.columns(4)
row[0].metric(
    "Avg tunnel latency",
    f"{sum(latencies) / len(latencies):.2f} ms" if latencies else "n/a",
)
row[1].metric(
    "Avg packet loss",
    f"{sum(losses) / len(losses):.2f} %" if losses else "n/a",
)
row[2].metric(
    "Avg bandwidth",
    f"{sum(bandwidths) / len(bandwidths):.2f} Mbps" if bandwidths else "n/a",
)
row[3].metric(
    "Tunnels in WARNING/CRITICAL",
    state_counts["WARNING"] + state_counts["CRITICAL"] + state_counts["UNREACHABLE"],
)

gauge_col, sla_col = st.columns([1, 2])

with gauge_col:
    # Health score is weighted by how the agent itself grades a tunnel:
    # NORMAL counts fully, WARNING half, CRITICAL/UNREACHABLE zero.
    if sla_rows:
        score = 100.0 * (
            state_counts["NORMAL"] + 0.5 * state_counts["WARNING"]
        ) / len(sla_rows)

        bar = "#2ecc71" if score >= 80 else "#f39c12" if score >= 50 else "#e74c3c"

        gauge = go.Figure(go.Indicator(
            mode="gauge+number",
            value=round(score, 1),
            title={"text": "Overlay health score"},
            number={"suffix": " %"},
            gauge={
                "axis": {"range": [0, 100]},
                "bar": {"color": bar},
                "steps": [
                    {"range": [0, 50], "color": "#fadbd8"},
                    {"range": [50, 80], "color": "#fdebd0"},
                    {"range": [80, 100], "color": "#d5f5e3"},
                ],
            },
        ))
        gauge.update_layout(height=250, margin=dict(l=20, r=20, t=50, b=10))
        st.plotly_chart(gauge, width="stretch")
    else:
        st.info("No tunnel SLA state recorded yet.")

with sla_col:
    st.markdown("**Per-tunnel SLA state** (as graded by the telemetry agent)")

    if not sla_rows:
        st.info(
            "sla_status is empty — start the telemetry agent "
            "(`python3 -m agents.telemetry_agent_runner`)."
        )
    else:
        table = []
        for (a, b), entry in sorted(sla_rows.items()):
            key = (str(a), str(b))
            table.append({
                "Tunnel": f"s{a} <-> s{b}",
                "State": entry["state"],
                "Latency (ms)": latest_latency.get(key),
                "Loss (%)": latest_loss.get(key),
                "Bandwidth (Mbps)": latest_bandwidth.get(key),
                "Since": pd.to_datetime(entry["last_change"], unit="s"),
            })

        frame = pd.DataFrame(table)

        def _state_style(record):
            color = STATE_COLOR.get(record["State"], "#95a5a6")
            return [f"color: {color}; font-weight: 600"
                    if column == "State" else ""
                    for column in record.index]

        st.dataframe(
            frame.style.apply(_state_style, axis=1).format(
                {"Latency (ms)": "{:.2f}", "Loss (%)": "{:.2f}",
                 "Bandwidth (Mbps)": "{:.2f}"},
                na_rep="—",
            ),
            width="stretch",
            hide_index=True,
        )

st.divider()


# ======================================================================
# Section 2 -- Topology
# ======================================================================

st.header("Live Topology")

if not topo_ok:
    st.info(
        "No switches discovered. Check that Ryu is running with "
        "`--observe-links` and that the topology is up."
    )
else:
    view = st.radio(
        "View",
        ["Underlay (physical links)", "Overlay (VXLAN tunnels)"],
        horizontal=True,
        label_visibility="collapsed",
    )
    overlay = view.startswith("Overlay")

    pos = fixed_positions(topology.graph)
    fig = go.Figure()

    faulted_pairs = {tuple(sorted(w["pair"])) for w in active_faults}

    if overlay:
        edges = []
        for tunnel in db.get_tunnels():
            a, b = tunnel["switch_a"], tunnel["switch_b"]
            if f"s{a}" in pos and f"s{b}" in pos:
                edges.append((f"s{a}", f"s{b}", (a, b)))
    else:
        edges = []
        for u, v, data in topology.graph.edges(data=True):
            pair = None
            if data.get("dst_switch") is not None:
                pair = (data["src_switch"], data["dst_switch"])
            edges.append((u, v, pair))

    for u, v, pair in edges:
        if u not in pos or v not in pos:
            continue

        key = tuple(sorted(pair)) if pair else None
        state = sla_rows.get(key, {}).get("state", "UNKNOWN") if key else "UNKNOWN"

        # A physical link has no SLA state of its own; draw it neutral
        # unless a fault is currently injected on it.
        if overlay:
            color = STATE_COLOR.get(state, STATE_COLOR["UNKNOWN"])
        else:
            color = "#34495e"

        faulted = key in faulted_pairs if key else False
        if faulted:
            color = "#e74c3c"

        x0, y0 = pos[u]
        x1, y1 = pos[v]

        label = f"{u} <-> {v}"
        if overlay:
            label += f" [{state}]"
        if faulted:
            label += "  ⚡ FAULT INJECTED"

        fig.add_trace(go.Scatter(
            x=[x0, x1], y=[y0, y1],
            mode="lines",
            line=dict(
                width=6 if faulted else 3,
                color=color,
                dash="dash" if overlay else "solid",
            ),
            hoverinfo="text",
            text=label,
            showlegend=False,
        ))

        if faulted:
            # A marker at the midpoint so an impaired link is findable
            # even when the two endpoints sit close together.
            fig.add_trace(go.Scatter(
                x=[(x0 + x1) / 2], y=[(y0 + y1) / 2],
                mode="markers+text",
                marker=dict(size=16, color="#e74c3c", symbol="x"),
                text=["FAULT"],
                textposition="bottom center",
                textfont=dict(color="#c0392b", size=10),
                hoverinfo="skip",
                showlegend=False,
            ))

    node_x, node_y, node_text, node_color, node_hover = [], [], [], [], []

    for node, data in topology.graph.nodes(data=True):
        if node not in pos:
            continue
        x, y = pos[node]
        node_x.append(x)
        node_y.append(y)
        node_text.append(node)

        is_switch = data.get("type") == "switch"
        node_color.append("#2980b9" if is_switch else "#8e44ad")

        degree = topology.graph.degree(node)
        node_hover.append(
            f"{node} ({'switch' if is_switch else 'host'}), {degree} link(s)"
        )

    fig.add_trace(go.Scatter(
        x=node_x, y=node_y,
        mode="markers+text",
        text=node_text,
        textposition="middle center",
        textfont=dict(color="white", size=11),
        marker=dict(size=38, color=node_color, line=dict(width=2, color="white")),
        hoverinfo="text",
        hovertext=node_hover,
        showlegend=False,
    ))

    fig.update_layout(
        height=560,
        xaxis=dict(visible=False, range=[-1.75, 1.75]),
        yaxis=dict(visible=False, range=[-1.6, 1.6], scaleanchor="x"),
        plot_bgcolor="white",
        margin=dict(l=10, r=10, t=10, b=10),
    )

    st.plotly_chart(fig, width="stretch")

    if overlay:
        st.caption(
            "🟢 NORMAL · 🟠 WARNING · 🔴 CRITICAL · 🟣 UNREACHABLE · "
            "grey UNKNOWN (no state yet). Thick red with an ✕ marks a "
            "link with a fault injected right now. "
            "Node positions are derived from the dpid, so they never "
            "move between refreshes."
        )
    else:
        st.caption(
            "Physical links discovered by Ryu. They carry no SLA state "
            "of their own — switch to the overlay view for tunnel "
            "health. Thick red with an ✕ marks an injected fault."
        )

st.divider()


# ======================================================================
# Section 3 -- Metrics
# ======================================================================

st.header("Metrics")

st.caption(
    f"Last {window_minutes} min from Prometheus. Red bands are fault "
    "windows reconstructed from chaos_log."
)


def metric_chart(title, query, label_keys, unit, thresholds=(), joiner=" <-> "):
    """One Prometheus metric as a time series, with fault shading."""
    frame, error = prom_range(query, window_minutes, label_keys, joiner)

    st.markdown(f"**{title}**")

    if error:
        st.warning(f"Prometheus query failed: {error}")
        return

    if frame.empty:
        st.info(f"No data for `{query}` in this window.")
        return

    shapes, annotations = fault_shapes(windows, now)

    for value, name, color in thresholds:
        shapes.append({
            "type": "line",
            "xref": "paper", "yref": "y",
            "x0": 0, "x1": 1, "y0": value, "y1": value,
            "line": {"color": color, "dash": "dot", "width": 1},
        })
        annotations.append({
            "x": 1, "y": value,
            "xref": "paper", "yref": "y",
            "text": f"{name} ({value} {unit})",
            "showarrow": False,
            "xanchor": "right", "yanchor": "bottom",
            "font": {"size": 10, "color": color},
        })

    # Traces and layout are handed to the constructor in one shot; adding
    # them incrementally makes Plotly revalidate the figure each time.
    fig = go.Figure(
        data=[
            go.Scatter(
                x=group["time"], y=group["value"],
                mode="lines", name=label,
                hovertemplate=f"%{{y:.2f}} {unit}<br>%{{x}}<extra>{label}</extra>",
            )
            for label, group in frame.sort_values("time").groupby("series", sort=False)
        ],
        layout={
            "height": 320,
            "margin": {"l": 10, "r": 10, "t": 30, "b": 10},
            "hovermode": "x unified",
            "plot_bgcolor": "white",
            "legend": {"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
            "xaxis": {"gridcolor": "#ecf0f1"},
            "yaxis": {"gridcolor": "#ecf0f1", "title": {"text": unit}},
            "shapes": shapes,
            "annotations": annotations,
        },
    )

    st.plotly_chart(fig, width="stretch")


sla_tab, link_tab, up_tab = st.tabs(
    ["End-to-end tunnel SLA", "Per-hop link metrics", "Tunnel availability"]
)

with sla_tab:
    st.caption(
        "What an overlay tunnel actually delivers end to end, measured "
        "by telemetry/sla_collector.py inside the edge namespaces. "
        "Dotted lines are the SLA thresholds from telemetry/config.py."
    )

    left, right = st.columns(2)

    with left:
        metric_chart(
            "Latency", "tunnel_latency_ms", ("switch_a", "switch_b"), "ms",
            thresholds=[
                (config.SLA_LATENCY_WARNING_MS, "warning", "#f39c12"),
                (config.SLA_LATENCY_CRITICAL_MS, "critical", "#e74c3c"),
            ],
        )
        metric_chart(
            "Packet loss", "tunnel_packet_loss_percent",
            ("switch_a", "switch_b"), "%",
            thresholds=[
                (config.SLA_LOSS_WARNING_PERCENT, "warning", "#f39c12"),
                (config.SLA_LOSS_CRITICAL_PERCENT, "critical", "#e74c3c"),
            ],
        )

    with right:
        metric_chart(
            "Jitter", "tunnel_jitter_ms", ("switch_a", "switch_b"), "ms",
            thresholds=[
                (config.SLA_JITTER_WARNING_MS, "warning", "#f39c12"),
                (config.SLA_JITTER_CRITICAL_MS, "critical", "#e74c3c"),
            ],
        )
        metric_chart(
            "Bandwidth", "tunnel_bandwidth_mbps", ("switch_a", "switch_b"), "Mbps",
            thresholds=[
                (config.SLA_MIN_BANDWIDTH_WARNING_MBPS, "warning", "#f39c12"),
                (config.SLA_MIN_BANDWIDTH_CRITICAL_MBPS, "critical", "#e74c3c"),
            ],
        )

    st.caption(
        "Bandwidth is a delivered-rate probe at the "
        f"{config.IPERF_BANDWIDTH} offered rate, not a capacity test, "
        f"and only {config.BANDWIDTH_TUNNELS_PER_ROUND} tunnel(s) are "
        f"probed per round — so each tunnel's line updates roughly every "
        f"{config.BANDWIDTH_CHECK_INTERVAL_S}s, in rotation."
    )

with link_tab:
    st.caption(
        "Per-hop metrics from the Ryu app (controller/link_metrics_app.py). "
        "These grade individual hops; the SLA tab grades what a whole "
        "tunnel path delivers."
    )

    left, right = st.columns(2)

    with left:
        metric_chart(
            "Link latency", "link_latency_ms", ("src_dpid", "dst_dpid"), "ms",
            joiner=" -> ",
        )
        metric_chart(
            "Link packet loss", "link_packet_loss_percent",
            ("src_dpid", "dst_dpid"), "%", joiner=" -> ",
        )

    with right:
        metric_chart(
            "Link jitter", "link_jitter_ms", ("src_dpid", "dst_dpid"), "ms",
            joiner=" -> ",
        )
        metric_chart(
            "Link utilization", "link_utilization_percent",
            ("src_dpid", "dst_dpid"), "%", joiner=" -> ",
        )

with up_tab:
    st.caption(
        "tunnel_up is 1 when the last probe got a reply. A line that "
        "stops entirely means the collector itself went away, which is "
        "a different failure from a tunnel going down."
    )
    metric_chart(
        "Tunnel reachability", "tunnel_up", ("switch_a", "switch_b"), "up",
    )

st.divider()


# ======================================================================
# Section 4 -- Chaos / tc control panel
# ======================================================================

st.header("Fault Injection (tc netem)")

st.caption(
    "Shells out to `sudo tc` / `sudo ip` on the interfaces backing the "
    "selected link. Impairment is applied to BOTH ends, so it affects "
    "traffic in either direction."
)

inter_switch_links = []
if topo_ok:
    for u, v, data in topology.graph.edges(data=True):
        if data.get("dst_switch") is not None:
            a, b = data["src_switch"], data["dst_switch"]
            if (a, b) not in [(x, y) for x, y, _ in inter_switch_links]:
                inter_switch_links.append((a, b, f"s{a} <-> s{b}"))

if not inter_switch_links:
    st.warning("No inter-switch links discovered — nothing to impair.")
else:
    injector = get_injector()

    label = st.selectbox(
        "Target link", [text for _, _, text in inter_switch_links]
    )
    switch_a, switch_b, _ = next(
        entry for entry in inter_switch_links if entry[2] == label
    )

    is_faulted = tuple(sorted((switch_a, switch_b))) in {
        tuple(sorted(w["pair"])) for w in active_faults
    }
    if is_faulted:
        st.warning(f"s{switch_a} <-> s{switch_b} already has an active fault.")

    cols = st.columns(4)
    delay = cols[0].number_input("Delay (ms)", 0, 5000, 0, step=10)
    jitter = cols[1].number_input("Jitter (ms)", 0, 1000, 0, step=5)
    loss = cols[2].number_input("Loss (%)", 0.0, 100.0, 0.0, step=0.5)
    rate = cols[3].number_input("Rate limit (Mbit/s)", 0.0, 1000.0, 0.0, step=1.0)

    duration = st.slider(
        "Auto-clear after (s) — 0 keeps the fault until cleared by hand",
        0, 300, 60,
    )

    buttons = st.columns(4)

    if buttons[0].button("Inject fault", width="stretch", type="primary"):
        kwargs = {
            "delay_ms": delay or None,
            "jitter_ms": jitter or None,
            "loss_percent": loss or None,
            "rate_mbit": rate or None,
        }

        if not any(value is not None for value in kwargs.values()):
            st.error("Set at least one of delay / jitter / loss / rate.")
        elif duration > 0:

            threading.Thread(
                target=injector.inject_temporary_fault,
                args=(switch_a, switch_b, duration),
                kwargs=kwargs,
                daemon=True,
            ).start()
            st.success(
                f"Injecting on s{switch_a} <-> s{switch_b} for {duration}s "
                "(auto-clears)."
            )
            time.sleep(1)
            st.rerun()
        else:
            ok = injector.inject_link_fault(switch_a, switch_b, **kwargs)
            if ok:
                st.success(
                    f"Fault applied to s{switch_a} <-> s{switch_b} "
                    "— stays until cleared."
                )
                st.rerun()
            else:
                st.error(
                    "Injection failed. Check passwordless sudo for tc and "
                    "that the link resolves to real interfaces."
                )

    if buttons[1].button("Clear this link", width="stretch"):
        injector.clear_link_fault(switch_a, switch_b)
        st.success(f"Cleared netem on s{switch_a} <-> s{switch_b}.")
        st.rerun()

    if buttons[2].button("Link DOWN", width="stretch"):
        if injector.set_link_state(switch_a, switch_b, up=False):
            st.success(f"s{switch_a} <-> s{switch_b} brought down.")
        else:
            st.error("Could not bring the link down.")
        st.rerun()

    if buttons[3].button("Link UP", width="stretch"):
        if injector.set_link_state(switch_a, switch_b, up=True):
            st.success(f"s{switch_a} <-> s{switch_b} restored.")
        else:
            st.error("Could not restore the link.")
        st.rerun()

    st.divider()

    if st.button("Clear ALL faults on every link"):
        get_injector().clear_all_faults()
        st.success("All netem impairments removed.")
        st.rerun()

st.markdown("#### Fault history")

if not windows:
    st.info("No faults injected yet.")
else:
    history = pd.DataFrame([
        {
            "Link": w["label"],
            "Type": w["fault_type"],
            "Impairment": describe_fault(w),
            "Started": pd.to_datetime(w["start"], unit="s"),
            "Ended": pd.to_datetime(w["end"], unit="s") if w["end"] else None,
            "Duration (s)": round((w["end"] or now) - w["start"], 1),
            "Active": w["end"] is None,
        }
        for w in sorted(windows, key=lambda w: w["start"], reverse=True)
    ])
    st.dataframe(history, width="stretch", hide_index=True, height=240)

st.divider()


# ======================================================================
# Section 5 -- Agent decisions
# ======================================================================

st.header("Agent Decisions")

decisions = db.get_recent_agent_decisions(limit=200)

st.subheader("Pipeline")

stages = ["Telemetry", "SLA grading", "RL decision", "Guardrail", "Execution"]

if not decisions:
    stage_status = ["idle"] * len(stages)
else:
    last = decisions[0]
    approved = bool(last["approved"])
    executed = bool(last["executed"])
    # A row existing at all means the first three stages ran to produce
    # it; only the last two can fail independently.
    stage_status = ["done", "done", "done"]
    stage_status.append("done" if approved else "rejected")
    stage_status.append(
        "done" if executed else ("skipped" if approved else "rejected")
    )

icons = {"done": "🟢", "rejected": "🔴", "skipped": "⚪", "idle": "⚪"}

pipeline_cols = st.columns(len(stages))
for column, stage, status in zip(pipeline_cols, stages, stage_status):
    column.markdown(f"### {icons[status]}\n**{stage}**")

st.caption(
    "State of the most recent decide → guardrail → execute run. "
    "🟢 completed · 🔴 rejected or failed here · ⚪ skipped or idle."
)

if not decisions:
    st.info(
        "No decisions recorded yet. Every WARNING/CRITICAL transition "
        "the agent handles lands here — inject a fault above to produce "
        "one."
    )
else:
    st.subheader("Most recent decision")

    last = decisions[0]

    try:
        target = json.loads(last["target"] or "{}")
    except (ValueError, TypeError):
        target = {}

    steps = [
        f"**Anomaly** — {last['metric_type']} graded `{last['anomaly_state']}`",
        f"**Proposed action** — `{last['action']}`: "
        f"{last['reasoning'] or 'no reasoning recorded'}",
        f"**Guardrail** — {'approved' if last['approved'] else 'rejected'}: "
        f"{last['guardrail_reason'] or 'no reason recorded'}",
    ]
    if last["approved"]:
        steps.append(
            f"**Execution** — {'executed' if last['executed'] else 'not executed'}: "
            f"{last['execution_result'] or 'no result recorded'}"
        )

    for index, step in enumerate(steps, start=1):
        st.markdown(f"{index}. {step}")

    st.caption(
        f"Decided at {pd.to_datetime(last['timestamp'], unit='s')} "
        f"({int(now - last['timestamp'])}s ago)."
    )

    if target:
        with st.expander("Target detail"):
            st.json(target)

    st.subheader("Decision log")

    frame = pd.DataFrame(decisions)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="s")
    frame["approved"] = frame["approved"].map({1: "yes", 0: "no"})
    frame["executed"] = frame["executed"].map({1: "yes", 0: "no"})
    frame = frame.rename(columns={
        "timestamp": "Time", "metric_type": "Metric", "anomaly_state": "State",
        "action": "Action", "reasoning": "Reasoning", "approved": "Approved",
        "guardrail_reason": "Guardrail", "executed": "Executed",
        "execution_result": "Result",
    }).drop(columns=["id", "target"])

    st.dataframe(frame, width="stretch", hide_index=True, height=320)

    st.download_button(
        "Export decisions as CSV",
        data=frame.to_csv(index=False).encode("utf-8"),
        file_name="agent_decisions.csv",
        mime="text/csv",
    )

st.divider()


# ======================================================================
# Section 6 -- Event log
# ======================================================================

st.header("Event Log")

limit = st.slider("Events shown", 10, 500, 100)
events = db.get_recent_events(limit=limit)

if not events:
    st.info("No events recorded yet.")
else:
    frame = pd.DataFrame(events)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="s")
    frame = frame.rename(columns={
        "timestamp": "Time", "event_type": "Type", "switch_id": "Switch",
        "port_no": "Port", "severity": "Severity", "description": "Description",
    }).drop(columns=["id"])

    severity_filter = st.multiselect(
        "Severity",
        sorted(frame["Severity"].dropna().unique()),
        default=sorted(frame["Severity"].dropna().unique()),
    )
    frame = frame[frame["Severity"].isin(severity_filter)]

    def _severity_style(record):
        style = SEVERITY_STYLE.get(record["Severity"], "")
        return [style] * len(record)

    st.dataframe(
        frame.style.apply(_severity_style, axis=1),
        width="stretch",
        hide_index=True,
        height=420,
    )

    st.download_button(
        "Export events as CSV",
        data=frame.to_csv(index=False).encode("utf-8"),
        file_name="network_events.csv",
        mime="text/csv",
    )


# ----------------------------------------------------------------------
# Auto refresh -- sleep then rerun, rather than pulling in an extra
# dependency just for a timer.
# ----------------------------------------------------------------------

if auto_refresh:
    time.sleep(refresh_interval)
    st.rerun()
