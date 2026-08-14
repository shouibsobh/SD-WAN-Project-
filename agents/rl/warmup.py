"""Trains PathScorer offline so it doesn't start production guessing.

Two modes:

  1. Synthetic (default) -- random candidate paths per episode, picked
     by the same epsilon-greedy select() decision_node uses.

         python -m agents.rl.warmup
         python -m agents.rl.warmup --episodes 5000 --epsilon 0.2

  2. Replay (--from-db) -- rebuilds weights from scratch off every
     resolved, approved+executed rl_transitions row in timestamp order.
     For recovering from a divergence without discarding real history.

         python -m agents.rl.warmup --from-db

Both overwrite path_scorer_weights.json (backed up to .bak first) and
write a history JSON for plot_quality.py.
"""

import argparse
import json
import os
import random
import shutil

from agents.rl.path_scorer import PathScorer, DEFAULT_MODEL_PATH
from agents.rl.reward_tracker import REWARD_CLIP
from agents.nodes.metrics_provider import ANOMALY_STATES, features_from_raw

DEFAULT_EPISODES = 3000
# Per-feature weights for the synthetic reward above. One slot per
# feature vector position: utilization, latency, jitter, loss.
W_UTILIZATION = 1.0
W_LATENCY = 1.0
W_JITTER = 0.5
W_LOSS = 1.0
DEFAULT_EPSILON = 0.2   # above production RL_EPSILON (0.1): warm-up
                        # wants feature-space coverage, not realism
DEFAULT_HISTORY_PATH = os.path.join(
    os.path.dirname(__file__), "warmup_history.json"
)

# hop_count and anomaly_state only shape the synthetic metrics; neither
# reaches the feature vector. PathScorer sees the same 4 measurements in
# warm-up as in production.


def _random_path_metrics(rng, hop_count):
    """Plausible aggregate metrics for a path with `hop_count` hops.

    Built so more hops tend to mean worse metrics, like real underlay
    paths -- otherwise there's no signal to learn, just noise.
    """
    per_hop_latency = max(0.5, rng.gauss(8, 6))
    per_hop_jitter = max(0.0, rng.gauss(2, 3))
    per_hop_loss = max(0.0, rng.uniform(0, 3))

    utilization = min(100.0, max(0.0, rng.gauss(35, 25)))
    latency_ms = per_hop_latency * hop_count + rng.uniform(0, 15)
    jitter_ms = min(100.0, per_hop_jitter * (hop_count ** 0.5) + rng.uniform(0, 5))
    loss_percent = min(100.0, max(0.0, per_hop_loss * hop_count + rng.gauss(0, 1)))

    return {
        "utilization_percent": utilization,
        "latency_ms": latency_ms,
        "jitter_ms": jitter_ms,
        "packet_loss_percent": loss_percent,
    }


def _features_from_metrics(metrics):
    """Route synthetic data through the same scaling as live features."""
    return features_from_raw(
        metrics["utilization_percent"], metrics["latency_ms"],
        metrics["jitter_ms"], metrics["packet_loss_percent"],
    )


def _reward_from_features(before, after):
    """Warm-up's own reward: improvement in the candidate's path metrics.

    Not reward_tracker's formula. That one scores a tunnel's end-to-end
    SLA, which needs a live network to measure -- there isn't one here.
    Warm-up only has to leave the weights with the right signs and rough
    magnitudes, so it scores in path-feature space directly. The live
    loop retrains from there.
    """
    return (
        W_UTILIZATION * (before[0] - after[0])
        + W_LATENCY * (before[1] - after[1])
        + W_JITTER * (before[2] - after[2])
        + W_LOSS * (before[3] - after[3])
    )


# ----------------------------------------------------------------
# Mode 1: synthetic warm-up
# ----------------------------------------------------------------

def run_synthetic_warmup(episodes, epsilon, seed=None,
                          min_candidates=2, max_candidates=5):

    rng = random.Random(seed)

    # rng only drives the synthetic metrics. PathScorer.select() draws
    # its epsilon roll and tie-breaks from the global RNG, so --seed
    # isn't reproducible unless that one is seeded too.
    if seed is not None:
        random.seed(seed)

    scorer = PathScorer.load_or_new()
    history = []

    for ep in range(episodes):

        anomaly_state = rng.choice(list(ANOMALY_STATES))
        n_candidates = rng.randint(min_candidates, max_candidates)
        shortest_hop_count = rng.randint(1, 3)

        options = []

        for i in range(n_candidates):
            # decision_node's convention: candidate 0 is the current
            # shortest path.
            hop_count = (
                shortest_hop_count if i == 0
                else shortest_hop_count + rng.randint(0, 3)
            )
            metrics = _random_path_metrics(rng, hop_count)
            features = _features_from_metrics(metrics)
            options.append({
                "action": "REROUTE", "target": {}, "features": features,
            })

        chosen, score, was_exploration = scorer.select(options, epsilon=epsilon)

        record = {
            "episode": ep,
            "anomaly_state": anomaly_state,
            "action": chosen["action"],
            "was_exploration": was_exploration,
            "score": score,
            "reward": None,
        }

        if chosen["action"] == "NONE":
            # Same as reward_tracker: no network change, no delta to
            # learn from.
            history.append(record)
            continue

        # "before" is candidate 0, the current shortest path, so the
        # reward measures the chosen path against doing nothing.
        before_features = options[0]["features"]
        after_features = chosen["features"]

        reward = _reward_from_features(before_features, after_features)
        reward = max(-REWARD_CLIP, min(REWARD_CLIP, reward))

        scorer.update(after_features, reward)

        record["reward"] = reward
        history.append(record)

        report_every = max(1, episodes // 20)
        if (ep + 1) % report_every == 0:
            recent = [h["reward"] for h in history[-200:] if h["reward"] is not None]
            avg = sum(recent) / len(recent) if recent else 0.0
            print(f"[warmup] episode {ep + 1}/{episodes}  "
                  f"avg_reward(last {len(recent)})={avg:.3f}")

    scorer.save()
    return scorer, history


# ----------------------------------------------------------------
# Mode 2: replay real history from the database
# ----------------------------------------------------------------

def run_db_replay():

    from telemetry.database import DatabaseManager

    db = DatabaseManager()
    # Fresh, not load_or_new(): this rebuilds weights from history
    # rather than adding to whatever is on disk.
    scorer = PathScorer()

    rows = db.get_rl_transitions(resolved_only=True, limit=100000)
    rows.sort(key=lambda r: r["timestamp"])

    history = []

    for row in rows:

        # reward_tracker's filter: only approved+executed rows changed
        # the network, so only those are worth learning from.
        if not (row.get("approved") and row.get("executed")):
            continue

        reward = row.get("reward")
        if reward is None:
            continue

        features = json.loads(row["features"])
        scorer.update(features, reward)

        history.append({
            "episode": len(history),
            "anomaly_state": row.get("anomaly_state"),
            "action": row.get("action"),
            "was_exploration": None,   # not recorded at insert time
            "score": None,
            "reward": reward,
        })

    scorer.save()

    print(f"[warmup] replayed {len(history)} real approved+executed "
          f"transitions from the database "
          f"({len(rows)} resolved rows total, "
          f"{len(rows) - len(history)} skipped as rejected/failed/unresolved)")

    return scorer, history


# ----------------------------------------------------------------
# CLI
# ----------------------------------------------------------------

def _backup_weights(path=DEFAULT_MODEL_PATH):
    if os.path.exists(path):
        backup_path = path + ".bak"
        shutil.copy(path, backup_path)
        print(f"[warmup] backed up existing weights to {backup_path}")


def main():

    parser = argparse.ArgumentParser(
        description="Warm up (or rebuild) PathScorer before/instead of "
                    "live online training"
    )
    parser.add_argument("--episodes", type=int, default=DEFAULT_EPISODES,
                        help="number of synthetic episodes (ignored with --from-db)")
    parser.add_argument("--epsilon", type=float, default=DEFAULT_EPSILON,
                        help="epsilon-greedy exploration rate during warm-up")
    parser.add_argument("--seed", type=int, default=None,
                        help="RNG seed for reproducible synthetic warm-up")
    parser.add_argument("--from-db", action="store_true",
                        help="rebuild weights from scratch by replaying real "
                             "resolved rl_transitions in telemetry.db, instead "
                             "of generating synthetic data")
    parser.add_argument("--history-out", default=DEFAULT_HISTORY_PATH,
                        help="where to write the JSON history for plot_quality.py")
    args = parser.parse_args()

    _backup_weights()

    if args.from_db:
        scorer, history = run_db_replay()
    else:
        scorer, history = run_synthetic_warmup(
            args.episodes, args.epsilon, args.seed
        )

    with open(args.history_out, "w") as f:
        json.dump(history, f)

    resolved_rewards = [h["reward"] for h in history if h["reward"] is not None]
    avg_reward = sum(resolved_rewards) / len(resolved_rewards) if resolved_rewards else 0.0

    print(f"[warmup] done -- {len(history)} episodes, "
          f"avg_reward={avg_reward:.3f}, "
          f"weights saved to {DEFAULT_MODEL_PATH}, "
          f"history saved to {args.history_out}")
    print("[warmup] run `python -m agents.rl.plot_quality` to visualize this")


if __name__ == "__main__":
    main()