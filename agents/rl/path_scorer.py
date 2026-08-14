"""Scores candidate REROUTE paths and picks one, epsilon-greedy.

Model: a linear function approximator, Q(features) = w . x + b, trained
online by SGD on the (features, reward) pairs reward_tracker.py resolves
in rl_transitions. Deliberately simple -- it persists as plain JSON, and
callers only see .select()/.update(), so swapping in something heavier
later touches nothing else.

"NONE" gets a fixed baseline score instead of learned weights: doing
nothing produces no before/after delta to fit.
"""

import json
import os
import random

FEATURE_SIZE = 4           # matches metrics_provider's vector:
                           # utilization, latency, jitter, loss
NONE_BASELINE_SCORE = 0.0  # raise to make the agent prefer NONE
LEARNING_RATE = 0.01

ERROR_CLIP = 50.0
WEIGHT_CLIP = 50.0

DEFAULT_MODEL_PATH = os.path.join(
    os.path.dirname(__file__), "path_scorer_weights.json"
)


class PathScorer:

    def __init__(self, weights=None, bias=0.0):

        self.weights = weights or [
            random.uniform(-0.01, 0.01) for _ in range(FEATURE_SIZE)
        ]
        self.bias = bias

    # -- persistence --

    @classmethod
    def load_or_new(cls, path=DEFAULT_MODEL_PATH):

        if not os.path.exists(path):
            return cls()

        try:
            with open(path) as f:
                data = json.load(f)
            return cls(weights=data["weights"], bias=data["bias"])
        except (json.JSONDecodeError, KeyError, OSError):
            return cls()

    def save(self, path=DEFAULT_MODEL_PATH):
        with open(path, "w") as f:
            json.dump({"weights": self.weights, "bias": self.bias}, f)

    # -- scoring --

    def predict(self, features):
        return sum(w * x for w, x in zip(self.weights, features)) + self.bias

    def select(self, options, epsilon=0.1):


        candidates = list(options)

        if not any(c["action"] == "NONE" for c in candidates):
            candidates.append(
                {"action": "NONE", "target": {}, "features": None}
            )

        scores = [
            NONE_BASELINE_SCORE if c["features"] is None else self.predict(c["features"])
            for c in candidates
        ]

        if random.random() < epsilon:
            idx = random.randrange(len(candidates))
            was_exploration = True
        else:
            best_score = max(scores)
            best_indices = [i for i, s in enumerate(scores) if s == best_score]
            # Random tie-break, so ties don't always go to the first
            # (shortest-path) candidate.
            idx = random.choice(best_indices)
            was_exploration = False

        return candidates[idx], scores[idx], was_exploration

    # -- one SGD step per resolved transition --

    def update(self, features, reward, learning_rate=LEARNING_RATE):

        predicted = self.predict(features)
        error = reward - predicted

        # Clip before applying: an outsized reward, or a prediction that
        # has already drifted, can otherwise start a runaway loop across
        # successive steps.
        error = max(-ERROR_CLIP, min(ERROR_CLIP, error))

        self.weights = [
            max(-WEIGHT_CLIP, min(WEIGHT_CLIP, w + learning_rate * error * x))
            for w, x in zip(self.weights, features)
        ]
        self.bias = max(-WEIGHT_CLIP, min(WEIGHT_CLIP, self.bias + learning_rate * error))