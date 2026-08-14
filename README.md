# Self-Healing SD-WAN with RL Agent 🛰️

A **Software-Defined WAN (SD-WAN)** project capable of **monitoring and healing itself
automatically**: the telemetry layer continuously monitors the health of VXLAN tunnels between
switches, and when it detects an SLA degradation (latency, jitter, packet loss, or a full outage)
an AI agent built on **LangGraph** analyzes the situation, picks an alternative path using a
simple **Reinforcement Learning (RL)** model, verifies the decision's safety through a
"guardrail" node, then executes it by installing new OpenFlow flows on the Ryu controller.
Everything is visualized live in an interactive **Streamlit** dashboard, alongside tools for
injecting synthetic faults (**Chaos Engineering**) to test the system's resilience.

---

## 1. Project Structure (Folder Hierarchy)
```
project-root/
│
├── controller/                     # Ryu SDN Controller apps (OpenFlow)
│   ├── topology_aware_switch.py    # Topology-aware L2 switch + Proxy ARP + REST API
│   └── link_metrics_app.py         # Per-link latency/jitter/utilization/loss measurement
│
├── telemetry/                      # Data collection + underlay network management layer
│   ├── config.py                   # All constants, SLA thresholds, and poll intervals
│   ├── database.py                 # SQLite layer (events, decisions, tunnels, RL transitions)
│   ├── ryu_client.py               # HTTP client for Ryu's REST API
│   ├── topology_manager.py         # Builds a NetworkX graph of the topology and computes paths
│   ├── tunnel_manager.py           # Creates/maintains a full-mesh VXLAN tunnel overlay
│   ├── sla_collector.py            # Measures real end-to-end tunnel SLA (ping + iperf3)
│   └── state_debouncer.py          # Prevents premature reactions by confirming state changes
│
├── agents/                         # The AI agent (Decide → Guardrail → Execute) via LangGraph
│   ├── state.py                    # Graph state definitions (AgentState / GraphState)
│   ├── graph.py                    # Builds the LangGraph: node sequence + conditional routing
│   ├── telemetry_agent.py          # Main loop: periodically checks tunnels + runs the graph
│   ├── telemetry_agent_runner.py   # Entry point to run TelemetryAgent as a standalone service
│   │
│   ├── nodes/                      # Graph nodes
│   │   ├── decision_node.py        # Proposes an action (REROUTE/NONE) via PathScorer (RL)
│   │   ├── guardrail_node.py       # Validates the decision against the live network before executing
│   │   ├── execution_node.py       # Executes the decision by installing OpenFlow flows on switches
│   │   ├── active_path.py          # Determines the currently active path (live flows or history)
│   │   └── metrics_provider.py     # Converts raw Prometheus metrics into a feature vector
│   │
│   └── rl/                         # Reinforcement learning model
│       ├── path_scorer.py          # Simple linear model Q(features)=w·x+b with SGD + epsilon-greedy
│       ├── path_scorer_weights.json# Saved model weights (updated automatically)
│       ├── reward_tracker.py       # Computes the actual reward after a reroute and updates the model
│       └── warmup.py               # Pre-trains the model (synthetic or DB replay) before going live
│
├── chaos/                          # Chaos engineering tools
│   ├── fault_injector.py           # Injects/clears faults via tc netem and ip link (delay, loss, down)
│   └── run_scenario.py             # CLI script to inject a specific fault for a set duration
│
├── dashboard/                      # Monitoring and control interface
│   └── app.py                      # Streamlit app: overview, live topology, metrics, decisions, events
│
└── database/                       # Created automatically on first run
    └── telemetry.db                # SQLite database (WAL mode)
└── topology.mn                     #Topology file (4 switches)
└── topology1.mn                    #Topology file(8 switches)
    ```

---

## 2. What Each Part Does

### a) `controller/` — Network Control Logic (Ryu Apps)

| File | Purpose |
|---|---|
| `topology_aware_switch.py` | A Ryu app that builds a topology graph, forwards packets along the shortest path (Dijkstra via NetworkX), prevents flooding loops with a software spanning tree, implements Proxy ARP, and exposes a REST API for the IP↔MAC table and the list of edge switches. |
| `link_metrics_app.py` | Measures, per physical link: **latency** (via custom timestamped LLDP-shaped probe frames), **jitter** (mean difference between consecutive samples), **utilization** (port-stat deltas over max speed), and **packet loss** (tx/rx delta). Exposes all of this as Prometheus metrics on port `9200`. |

### b) `telemetry/` — Measurement & Underlay Network Management

| File | Purpose |
|---|---|
| `config.py` | All constants: IP addresses, poll intervals, SLA thresholds (warning/critical) for latency, jitter, loss, and bandwidth, and VXLAN settings. |
| `database.py` | Manages the SQLite database with tables: `network_events`, `sla_status`, `agent_decisions`, `chaos_log`, `rl_transitions`, `tunnels`. |
| `ryu_client.py` | A thin HTTP client wrapper over Ryu's REST API (switches, links, hosts, flows). |
| `topology_manager.py` | Builds a `networkx.Graph` from Ryu's data and computes up to N alternative paths between two switches. |
| `tunnel_manager.py` | Builds and reconciles a full-mesh VXLAN overlay between all edge switches: creates network namespaces, Linux bridges, and VXLAN interfaces inside each namespace. |
| `sla_collector.py` | Measures real end-to-end tunnel SLA via `ping` (latency/jitter/loss) and `iperf3` (bandwidth), exposing them as Prometheus metrics on port `9201`. |
| `state_debouncer.py` | Prevents acting on a single transient reading; requires the same state to repeat twice in a row (`DEFAULT_CONFIRM_READS=2`) before it's trusted. |

### c) `agents/` — The AI Agent (LangGraph + RL)

**Main loop (`telemetry_agent.py` + `telemetry_agent_runner.py`):**
Probes all tunnels in parallel (thread pool), determines each tunnel's state
(`NORMAL/WARNING/CRITICAL/UNREACHABLE`) via `determine_sla_state`, and on any degradation runs
the `decide → guardrail → execute` graph, logs the outcome, and resolves pending RL rewards
every few seconds.

**The graph (`graph.py` + `state.py`):**
Built with **LangGraph** (`StateGraph`), consisting of three nodes with conditional routing after
`guardrail`:

```
decide → guardrail ──approved──▶ execute ──▶ END
              └──rejected──────────────────▶ END
```

**Nodes (`nodes/`):**
- **`decision_node.py`**: Builds the possible reroute options (alternative paths + a "stay"
  option), then uses `PathScorer` (the RL model) to pick the best one via **epsilon-greedy**, and
  logs an RL "transition" in the database to be evaluated later.
- **`guardrail_node.py`**: Verifies the proposed path is still valid (present among the
  candidates, and not already the active path) before approving execution — a last line of
  defense against stale or unsafe decisions.
- **`execution_node.py`**: Actually executes the decision by installing OpenFlow flows (higher
  priority than normal forwarding) on every switch along the new path, in both directions.
- **`active_path.py`**: Determines the actual current path — either by reading live flow tables
  from the switches, or by falling back to the last approved-and-executed decision in history.
- **`metrics_provider.py`**: Queries Prometheus and converts raw metrics into a unified feature
  vector (utilization, latency, jitter, loss) normalized between 0 and 1, used by the RL model.

**Reinforcement learning (`rl/`):**
- **`path_scorer.py`**: A **simple linear model** `Q(x) = w·x + b`, trained online via **SGD**,
  persisted as a JSON file (`path_scorer_weights.json`).
- **`reward_tracker.py`**: After a delay, compares SLA "before" and "after" a reroute and
  computes a weighted reward (loss weighted more than jitter, more than latency), then updates
  `PathScorer`'s weights.
- **`warmup.py`**: Pre-trains the model before going live, in two modes: synthetic random data,
  or a full **replay** of every real RL decision stored in the database.

### d) `chaos/` — Chaos Engineering

- **`fault_injector.py`**: Injects real faults on network interfaces via `tc qdisc netem`
  (delay/jitter/loss/rate limiting) or brings a link fully down (`ip link down`), logging every
  action to `chaos_log`.
- **`run_scenario.py`**: A CLI tool to inject a fault on a specific link for a given duration,
  bring a link fully down, or clear all active faults at once.

### e) `dashboard/app.py` — Control Dashboard (Streamlit)

An interactive dashboard with 6 sections: **Overview** (network health KPIs), **Live Topology**
(interactive Plotly graph of the underlay and VXLAN overlay), **Metrics** (time-series charts for
SLA and links), **Fault Injection panel**, **Agent Decisions** (tracks each stage of
`decide → guardrail → execute`), **Event Log**.

---

## 3. Required Technologies, Tools & Libraries

### Network infrastructure (must be provisioned externally before running)
- **containernet** or a similar SDN environment that creates the virtual switches and connects them
  to the controller.
- **Open vSwitch (OVS)** supporting **OpenFlow 1.3**.
- Linux system tools: `tc` (netem), `ip` (netns/link), and **passwordless `sudo`** for the user
  running the code (required by `sudo tc` and `sudo ip netns` commands in `fault_injector.py`
  and `tunnel_manager.py`).
- **iperf3** and **ping** installed on the system (for SLA measurements).

### Runtime services
- **Ryu SDN Framework** (`ryu-manager`) — to run the apps in `controller/*.py`.
- **Prometheus** — running on `localhost:9090`, configured to scrape ports `9200` (link metrics)
  and `9201` (tunnel SLA metrics).

### Required Python libraries 

```
langgraph
networkx
requests
prometheus_client
streamlit
plotly
pandas
ryu              # also brings webob and eventlet as dependencies
```


### Python version
Python 3.9+ (required for `TypedDict` with `total=False` and modern type-hint syntax).

---

## 4. How to Run (in order)

> Assumes the project root contains the packages as proper Python packages (`__init__.py` in
> `agents/`, `agents/nodes/`, `agents/rl/`, `telemetry/`, `chaos/`), and that all commands are run
> from the project root.

### Step 0 — Install dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install langgraph networkx requests prometheus_client streamlit plotly pandas ryu
```

### Step 1 — Start the SDN network (containernet + OVS)
Set up a Mininet topology (topology.mn and topology1.mn) that connects to the controller
on the default OpenFlow port (6653), with switches supporting OpenFlow 1.3.

### Step 2 — Start the Ryu controller with both apps
```bash
ryu-manager controller/topology_aware_switch.py controller/link_metrics_app.py \
    --observe-links
```
- Ryu's REST API (ofctl + topology) will run on port `8080`.
- Link metrics (`link_*`) will be exposed at `http://localhost:9200/metrics`.

### Step 3 — Start Prometheus
Configure Prometheus to scrape `localhost:9200` and `localhost:9201`, then run it on port `9090`.

### Step 4 — Build the VXLAN tunnel overlay
```bash
python3 -m telemetry.tunnel_manager
```
Automatically creates namespaces and VXLAN interfaces between every pair of edge switches, and
updates the `tunnels` table.

### Step 5 — (Optional) Pre-train the RL model
```bash
python3 -m agents.rl.warmup --episodes 3000        # synthetic data
# or
python3 -m agents.rl.warmup --from-db              # replay real past decisions
```

### Step 6 — Start the AI agent (monitoring + automatic decisions)
```bash
python3 -m agents.telemetry_agent_runner
```
This process periodically probes all tunnels (every `SLA_POLL_INTERVAL` seconds), and on any
degradation, automatically runs `decide → guardrail → execute`.

### Step 7 — Launch the dashboard
```bash
streamlit run dashboard/app.py
```

### Step 8 — (Optional) Inject faults to test the system
```bash
# Inject 100ms delay and 5% loss on the link between switches 1 and 2 for 30 seconds
python3 -m chaos.run_scenario --link 1 2 --delay 100 --loss 5 --duration 30

# Bring a link fully down for 20 seconds
python3 -m chaos.run_scenario --link 1 2 --down --duration 20

# Clear all active faults
python3 -m chaos.run_scenario --clear-all
```

---

## 5. Important Notes Before Running

- **Permissions**: the running user must have passwordless `sudo` access for `tc`, `ip link`,
  and `ip netns` commands (used in `tunnel_manager.py`, `fault_injector.py`, and
  `sla_collector.py`).
- **Order matters**: the topology and VXLAN tunnels must be up and running **before** starting
  `telemetry_agent`, otherwise there will be no candidate paths available for rerouting.
- **Database**: `database/telemetry.db` is created automatically (SQLite in WAL mode) on the
  first call to `DatabaseManager()` — no manual setup needed.
- **`RL_EPSILON` environment variable**: controls the RL model's random exploration rate during
  live operation (default `0.1`); can be overridden:
  `RL_EPSILON=0.2 python3 -m agents.telemetry_agent_runner`.
