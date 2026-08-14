import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from telemetry.database import DatabaseManager
from telemetry.topology_manager import TopologyManager
from telemetry.sla_collector import SlaCollector
from telemetry.state_debouncer import StateDebouncer
from telemetry import config
from agents.graph import build_graph
from agents.nodes.decision_node import get_scorer
from agents.rl.reward_tracker import resolve_pending_transitions

REWARD_CHECK_INTERVAL_S = 5

# Tunnels are probed concurrently, so a round costs the slowest single
# probe instead of the sum of all of them. Sequentially it grew with the
# mesh size (N*(N-1)/2 tunnels) and could outlast a 30s injected fault,
# which is how faults used to go undetected on the later tunnels.
SLA_MAX_PARALLEL_PROBES = 16


class TelemetryAgent:
    """Polls tunnel SLA and runs the decide -> guardrail -> execute graph."""

    def __init__(self):
        self.db = DatabaseManager()
        self.topology = TopologyManager()
        self.graph = build_graph()

        # Same PathScorer instance decision_node.py uses. reward_tracker
        # updates weights from observed outcomes, so a second copy here
        # would score against stale weights.
        self._rl_scorer = get_scorer()
        self._last_reward_check = 0.0

        # A reading only becomes a tunnel's state after DEFAULT_CONFIRM_READS
        # identical rounds, so one lost probe can't fire a spurious event.
        # Costs one extra round of detection latency. Shared by all tunnels;
        # its internal lock makes confirm() safe from the probe threads.
        self._debouncer = StateDebouncer()

        # ping/iperf3 over the VXLAN tunnels themselves -- end-to-end, unlike
        # the per-hop counters Prometheus scrapes off the controller.
        self._sla_collector = SlaCollector(export_to_prometheus=True)
        self._last_tunnel_check = 0.0
        self._last_bandwidth_check = 0.0
        self._bandwidth_cursor = 0

        self._sla_executor = ThreadPoolExecutor(
            max_workers=SLA_MAX_PARALLEL_PROBES,
            thread_name_prefix="sla-probe",
        )

    # -- SLA evaluation -------------------------------------------------

    def determine_sla_state(self, metrics):
        latency = metrics.get("latency_ms")
        jitter = metrics.get("jitter_ms")
        loss = metrics.get("packet_loss_percent")
        bandwidth = metrics.get("bandwidth_Mbps")

        if latency is None and loss is None and bandwidth is None:
            # Nothing came back at all: interface gone, or 100% loss.
            return "UNREACHABLE"

        def breaches(loss_limit, latency_limit, jitter_limit, bandwidth_floor):
            return (
                (loss is not None and loss >= loss_limit)
                or (latency is not None and latency >= latency_limit)
                or (jitter is not None and jitter >= jitter_limit)
                or (bandwidth is not None and bandwidth <= bandwidth_floor)
            )

        if breaches(config.SLA_LOSS_CRITICAL_PERCENT,
                    config.SLA_LATENCY_CRITICAL_MS,
                    config.SLA_JITTER_CRITICAL_MS,
                    config.SLA_MIN_BANDWIDTH_CRITICAL_MBPS):
            return "CRITICAL"

        if breaches(config.SLA_LOSS_WARNING_PERCENT,
                    config.SLA_LATENCY_WARNING_MS,
                    config.SLA_JITTER_WARNING_MS,
                    config.SLA_MIN_BANDWIDTH_WARNING_MBPS):
            return "WARNING"

        return "NORMAL"

    def build_sla_event(self, old_state, new_state, metrics):
        detail = (
            f"latency={metrics.get('latency_ms')} ms, "
            f"jitter={metrics.get('jitter_ms')} ms, "
            f"loss={metrics.get('packet_loss_percent')}%, "
            f"bandwidth={metrics.get('bandwidth_Mbps')} Mbps"
        )

        if new_state == "UNREACHABLE":
            return {
                "type": "TUNNEL_UNREACHABLE", "severity": "CRITICAL",
                "description": "No ping/iperf3 response over this tunnel "
                               "(endpoint interface missing or probe failed)",
            }
        if new_state == "WARNING":
            return {
                "type": "TUNNEL_DEGRADED", "severity": "WARNING",
                "description": f"Tunnel SLA degraded: {detail}",
            }
        if new_state == "CRITICAL":
            return {
                "type": "TUNNEL_SLA_VIOLATION", "severity": "CRITICAL",
                "description": f"Tunnel SLA violated: {detail}",
            }
        if new_state == "NORMAL" and old_state is not None:
            return {
                "type": "TUNNEL_RECOVERY", "severity": "INFO",
                "description": f"Tunnel SLA back to normal: {detail}",
            }
        return None

    def analyze_tunnel(self, switch_a, switch_b, metrics):
        """Debounce this round's reading, persist real changes, react.

        The debounce key is the sorted pair, the same identity
        guardrail_node._target_identity uses, so s1<->s3 and s3<->s1
        share one streak. Debouncing also stops a single lucky probe
        mid-fault from logging a false recovery, which would otherwise
        split one incident into two in the MTTR report.
        """
        candidate_state = self.determine_sla_state(metrics)

        key_a, key_b = f"tunnel:s{switch_a}", f"tunnel:s{switch_b}"
        confirmed_state = self._debouncer.confirm(
            tuple(sorted((key_a, key_b))), candidate_state
        )
        if confirmed_state is None:
            return                                  # still noise, wait a round

        old_state = self.db.get_sla_state(key_a, key_b)
        if old_state == confirmed_state:
            return

        new_state = confirmed_state
        self.db.update_sla_state(key_a, key_b, new_state)

        event = self.build_sla_event(old_state, new_state, metrics)
        if event is None:
            return

        self.db.insert_event(
            event["type"], switch_a, switch_b, event["severity"],
            f"tunnel s{switch_a} <-> s{switch_b}: {event['description']}",
        )
        print("[TUNNEL SLA EVENT]", f"s{switch_a} <-> s{switch_b}",
              old_state, "->", new_state)

        # The end-to-end degradation is the trigger; decision_node picks the
        # replacement path from the underlay link metrics.
        if new_state != "NORMAL":
            self._run_graph(
                {"switch_a": switch_a, "switch_b": switch_b, **metrics},
                new_state, "tunnel",
            )

    # -- decide -> guardrail -> execute ----------------------------------

    def _run_graph(self, metric, anomaly_state, metric_type):
        """Run the LangGraph pipeline and record the decision either way."""
        graph_input = dict(metric)
        graph_input["state"] = anomaly_state

        try:
            result = self.graph.invoke({
                "metric": graph_input, "metric_type": metric_type,
            })
        except Exception as exc:
            print(f"[GRAPH] pipeline error ({metric_type}): {exc}")
            self.db.insert_agent_decision({
                "timestamp": time.time(),
                "metric_type": metric_type,
                "anomaly_state": anomaly_state,
                "action": "ERROR",
                "target": "{}",
                "reasoning": f"Pipeline error: {exc}",
                "approved": False,
                "guardrail_reason": str(exc),
                "executed": False,
                "execution_result": None,
            })
            return

        proposed = result.get("proposed_action") or {}

        print("[GRAPH]", metric_type,
              "state:", anomaly_state,
              "action:", proposed.get("action"),
              "approved:", result.get("approved"),
              "executed:", result.get("executed"),
              "detail:", result.get("execution_result") or result.get("guardrail_reason"))

        # Backfill the row decision_node inserted before the guardrail ran.
        # Without this it stays approved=0/executed=0 and reward_tracker
        # resolves it at reward=0 with no SGD step -- so nothing learns.
        transition_id = result.get("rl_transition_id")
        if transition_id is not None:
            self.db.update_rl_transition_outcome(
                transition_id,
                approved=result.get("approved", False),
                executed=result.get("executed", False),
            )

        self.db.insert_agent_decision({
            "timestamp": time.time(),
            "metric_type": metric_type,
            "anomaly_state": anomaly_state,
            "action": proposed.get("action", "NONE"),
            "target": json.dumps(proposed.get("target", {}), default=str),
            "reasoning": result.get("reasoning", ""),
            "approved": result.get("approved", False),
            "guardrail_reason": result.get("guardrail_reason", ""),
            "executed": result.get("executed", False),
            "execution_result": result.get("execution_result"),
        })

    # -- probe rounds ----------------------------------------------------

    def _bandwidth_slice(self, tunnels):
        """Next few tunnels due an iperf3 run, advancing the cursor.

        SlaCollector serializes bandwidth probes (parallel iperf3 streams
        share the underlay and end up measuring each other), so the cost is
        linear in how many are probed while this blocks the loop. A rotating
        slice keeps each round bounded and still covers the whole mesh.
        """
        slice_size = min(config.BANDWIDTH_TUNNELS_PER_ROUND, len(tunnels))
        targets = set()

        for offset in range(slice_size):
            tunnel = tunnels[(self._bandwidth_cursor + offset) % len(tunnels)]
            targets.add((tunnel["switch_a"], tunnel["switch_b"]))

        self._bandwidth_cursor = (self._bandwidth_cursor + slice_size) % len(tunnels)
        return targets

    def _check_all_tunnels(self, with_bandwidth=False):
        """Probe every tunnel in parallel, analysing each as it lands."""
        tunnels = self.db.get_tunnels()
        if not tunnels:
            return

        bandwidth_targets = self._bandwidth_slice(tunnels) if with_bandwidth else set()

        future_to_tunnel = {
            self._sla_executor.submit(
                self._sla_collector.measure_tunnel,
                tunnel["switch_a"], tunnel["switch_b"],
                tunnel["remote_ip_a"], tunnel["remote_ip_b"],
                with_bandwidth=(tunnel["switch_a"], tunnel["switch_b"]) in bandwidth_targets,
            ): tunnel
            for tunnel in tunnels
        }

        for future in as_completed(future_to_tunnel):
            tunnel = future_to_tunnel[future]

            try:
                metrics = future.result()
            except Exception as exc:
                print(f"[telemetry] SLA probe failed for "
                      f"s{tunnel['switch_a']}<->s{tunnel['switch_b']}: {exc}")
                continue

            # Runs on the worker thread: sqlite is opened with
            # check_same_thread=False and the debouncer holds its own lock.
            self.analyze_tunnel(tunnel["switch_a"], tunnel["switch_b"], metrics)

    def run(self):
        while True:
            # decision_node builds its own TopologyManager, so this refresh is
            # only for whatever reads self.topology here.
            try:
                self.topology.discover()
            except Exception as exc:
                print(f"[telemetry] topology discovery failed: {exc}")

            if time.time() - self._last_tunnel_check >= config.SLA_POLL_INTERVAL:
                # Stamped from the start of the round, so a slow round doesn't
                # also delay the next one. iperf3 holds a link saturated for
                # IPERF_DURATION seconds, so bandwidth runs on its own slower
                # interval -- but it does have to run: without it
                # bandwidth_Mbps stays None and the throughput thresholds in
                # determine_sla_state can never fire.
                round_start = time.time()
                check_bandwidth = (
                    round_start - self._last_bandwidth_check
                    >= config.BANDWIDTH_CHECK_INTERVAL_S
                )

                self._check_all_tunnels(with_bandwidth=check_bandwidth)

                self._last_tunnel_check = round_start
                if check_bandwidth:
                    self._last_bandwidth_check = round_start

            # Pay out rewards for earlier reroutes (agents/rl/reward_tracker.py).
            if time.time() - self._last_reward_check >= REWARD_CHECK_INTERVAL_S:
                try:
                    resolve_pending_transitions(self._rl_scorer, self.topology, self.db)
                except Exception as exc:
                    print(f"[RL] reward resolution error: {exc}")
                self._last_reward_check = time.time()

            time.sleep(config.POLL_INTERVAL)
