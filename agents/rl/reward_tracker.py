

import json

from agents.nodes.metrics_provider import get_tunnel_sla, tunnel_sla_vector
from telemetry import config


REWARD_DELAY_S = 2 * config.SLA_POLL_INTERVAL

# Weights folding the three "lower is better" SLA deltas into one scalar.
# Loss above latency: a lossy tunnel is worse than a slow one.
W_LATENCY = 1.0
W_JITTER = 0.5
W_LOSS = 2.0


UNREACHABLE_RECOVERY_BONUS = 1.0


REWARD_CLIP = 20.0


def _tunnel_of(transition):
    """(switch_a, switch_b) for the transition's target, or None."""
    target = json.loads(transition["target"])
    switch_a, switch_b = target.get("switch_a"), target.get("switch_b")

    if switch_a is None or switch_b is None:
        return None

    return switch_a, switch_b


def _sla_before(transition):
 
    raw = transition.get("sla_before")

    if not raw:
        return None

    try:
        vector = json.loads(raw)
    except ValueError:
        return None

    if not isinstance(vector, list) or len(vector) != 3:
        return None

    return vector


def _reward_from_sla(before, after):

    weights = (W_LATENCY, W_JITTER, W_LOSS)

    terms = [
        weight * (b - a)
        for weight, b, a in zip(weights, before, after)
        if b is not None and a is not None
    ]

    if not terms:
        return None

    reward = sum(terms)

    was_unreachable = before[2] is not None and before[2] >= 1.0
    is_reachable = after[2] is not None and after[2] < 1.0

    if was_unreachable and is_reachable:
        reward += UNREACHABLE_RECOVERY_BONUS

    return reward


def resolve_pending_transitions(scorer, topology, db,
                                 reward_delay_s=REWARD_DELAY_S):

    pending = db.get_pending_rl_transitions(older_than_seconds=reward_delay_s)

    if not pending:
        return

    updated = False

    for transition in pending:

        if not (transition.get("approved") and transition.get("executed")):
            db.resolve_rl_transition(transition["id"], reward=0.0)
            continue

        tunnel = _tunnel_of(transition)
        before = _sla_before(transition)

        if tunnel is None or before is None:
            db.resolve_rl_transition(transition["id"], reward=0.0)
            continue

        after = tunnel_sla_vector(get_tunnel_sla(*tunnel))
        reward = _reward_from_sla(before, after)

        if reward is None:
            # Nothing measurable on either side; resolve at 0 rather
            # than leaving the row pending forever.
            db.resolve_rl_transition(transition["id"], reward=0.0)
            continue

        reward = max(-REWARD_CLIP, min(REWARD_CLIP, reward))

        db.resolve_rl_transition(transition["id"], reward=reward)
        scorer.update(json.loads(transition["features"]), reward)
        updated = True

    if updated:
        scorer.save()
