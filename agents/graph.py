from langgraph.graph import StateGraph, END

from agents.state import AgentState
from agents.nodes.decision_node import decision_node
from agents.nodes.guardrail_node import guardrail_node
from agents.nodes.execution_node import execution_node


def _route_after_guardrail(state: AgentState) -> str:
    return "execute" if state.get("approved") else "end"


def build_graph():

    workflow = StateGraph(AgentState)

    workflow.add_node("decide", decision_node)
    workflow.add_node("guardrail", guardrail_node)
    workflow.add_node("execute", execution_node)

    workflow.set_entry_point("decide")

    workflow.add_edge("decide", "guardrail")

    workflow.add_conditional_edges(
        "guardrail",
        _route_after_guardrail,
        {"execute": "execute", "end": END},
    )

    workflow.add_edge("execute", END)

    return workflow.compile()
