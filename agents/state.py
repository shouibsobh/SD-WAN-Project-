from typing import Any, Optional, TypedDict


class GraphState(TypedDict, total=False):

    # -- decision_node --
    reasoning: str
    proposed_action: Optional[dict[str, Any]]
    #   {"action": "REROUTE" | "NONE",
    #    "target": {"switch_a":..., "switch_b":..., "selected_path": [...]},
    #    "reasoning": "..."}
    candidate_paths: Optional[list]

    # -- guardrail_node --
    approved: bool
    guardrail_reason: str
    candidate_path: Optional[list]      # path the guardrail settled on; may
                                        # differ from decision_node's pick

    # -- execution_node --
    executed: bool
    execution_result: Optional[str]


class AgentState(GraphState, total=False):

    metric: dict[str, Any]              # carries metric["state"]
    metric_type: str                   # "tunnel"
    rl_transition_id: Optional[int]     # set for the reward tracker
