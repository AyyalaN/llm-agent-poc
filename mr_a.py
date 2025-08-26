# mr_agent.py
# A LangGraph stategraph agent specialized for Medical Records (MR).
# Compatible with the Google A2A sample agent_executor.py expectations.
from __future__ import annotations

import re
from typing import Any, AsyncGenerator, Annotated, TypedDict, Optional
from uuid import uuid4

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langchain_core.messages import HumanMessage, AIMessage


class MRState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    intent: str
    params: dict
    result: str


# ---- Hardcoded "tools" backed by in-memory dicts ----

_MR_DB: dict[str, dict[str, Any]] = {
    # pretend record IDs
    "MR-2001": {
        "patient_id": "P-5001",
        "diagnoses": ["Type 2 diabetes mellitus (E11.9)", "Hypertension (I10)"],
        "medications": [
            {"name": "Metformin", "dose": "500 mg", "sig": "BID"},
            {"name": "Lisinopril", "dose": "10 mg", "sig": "QD"},
        ],
        "allergies": ["Penicillin"],
        "assessment": "Glycemic control suboptimal; BP well controlled.",
        "plan": "Titrate metformin to 1000 mg BID if tolerated; diet and exercise counseling.",
        "summary": (
            "Patient with T2DM and HTN. A1C trending high; continue ACE inhibitor; "
            "reinforce lifestyle; consider metformin titration."
        ),
    },
    "MR-2002": {
        "patient_id": "P-5002",
        "diagnoses": ["Migraine without aura (G43.0)"],
        "medications": [{"name": "Sumatriptan", "dose": "50 mg", "sig": "PRN"}],
        "allergies": [],
        "assessment": "Episodic migraines; triggers include lack of sleep.",
        "plan": "Trigger avoidance; PRN triptan; consider prophylaxis if frequency increases.",
        "summary": "Episodic migraines; PRN therapy; monitor frequency.",
    },
}


def _get_record_id(text: str) -> Optional[str]:
    m = re.search(r"\bMR-\d{4}\b", text, flags=re.I)
    return m.group(0).upper() if m else None


def tool_get_record_item(record_id: str, field: str) -> str:
    rec = _MR_DB.get(record_id)
    if not rec:
        return f"Medical record {record_id} not found."
    if field not in rec:
        return f"Field '{field}' not found in {record_id}."
    val = rec[field]
    if isinstance(val, list):
        return ", ".join([v if isinstance(v, str) else f"{v}" for v in val])
    if isinstance(val, dict):
        return ", ".join([f"{k}: {v}" for k, v in val.items()])
    return str(val)


def tool_summarize_record(record_id: str) -> str:
    rec = _MR_DB.get(record_id)
    if not rec:
        return f"Medical record {record_id} not found."
    return rec.get("summary", f"No summary available for {record_id}.")


# ---- Node functions ----

def classify(state: MRState) -> MRState:
    """Very small rule-based classifier for demo purposes."""
    user_text = state["messages"][-1].content.lower()

    record_id = _get_record_id(user_text) or "MR-2001"  # default for demo
    # Which field is requested?
    if any(k in user_text for k in ("medication", "medications", "rx", "drug")):
        intent, field = "get_item", "medications"
    elif any(k in user_text for k in ("diagnosis", "diagnoses", "dx")):
        intent, field = "get_item", "diagnoses"
    elif any(k in user_text for k in ("assessment",)):
        intent, field = "get_item", "assessment"
    elif any(k in user_text for k in ("plan",)):
        intent, field = "get_item", "plan"
    elif any(k in user_text for k in ("allerg",)):
        intent, field = "get_item", "allergies"
    elif any(k in user_text for k in ("summar", "tl;dr", "overview")):
        intent, field = "summarize", ""
    else:
        # default: summarize if nothing obvious
        intent, field = "summarize", ""

    return {
        **state,
        "intent": intent,
        "params": {"record_id": record_id, "field": field},
    }


def fetch(state: MRState) -> MRState:
    intent = state["intent"]
    record_id = state["params"]["record_id"]
    field = state["params"]["field"]
    if intent == "get_item":
        text = tool_get_record_item(record_id, field)
        prefix = f"[MR Agent] {record_id} → {field}: "
        return {"result": prefix + text}
    else:
        text = tool_summarize_record(record_id)
        prefix = f"[MR Agent] {record_id} → summary: "
        return {"result": prefix + text}


def finalize(state: MRState) -> MRState:
    out = state["result"]
    return {"messages": [AIMessage(content=out)], "result": out}


# ---- Agent class ----

class MRAgent:
    """Medical Records agent (LangGraph stategraph).
    Provides:
      - invoke(query, sessionId=None) -> dict
      - stream(query, sessionId=None) -> async generator of dicts
    """
    # The sample __main__.py uses these on the AgentCard
    SUPPORTED_CONTENT_TYPES = ["text/plain"]

    def __init__(self) -> None:
        builder = StateGraph(MRState)
        builder.add_node("classify", classify)
        builder.add_node("fetch", fetch)
        builder.add_node("finalize", finalize)

        def router(s: MRState) -> str:
            return "fetch"

        builder.set_entry_point("classify")
        builder.add_conditional_edges("classify", router, {"fetch": "fetch"})
        builder.add_edge("fetch", "finalize")
        builder.add_edge("finalize", END)

        self.graph = builder.compile()

    def _mk_state(self, query: str) -> MRState:
        return {"messages": [HumanMessage(content=query)]}

    def invoke(self, query: str, sessionId: str | None = None) -> dict:
        """Synchronous, single-shot call used by on_message_send."""
        state = self._mk_state(query)
        result = self.graph.invoke(
            state,
            config={"configurable": {"thread_id": sessionId or str(uuid4())}},
        )
        return {
            "is_task_complete": True,
            "require_user_input": False,
            "content": result["result"],
        }

    async def stream(
        self, query: str, sessionId: str | None = None
    ) -> AsyncGenerator[dict, None]:
        """Streaming call used by on_message_stream; yields WORKING steps then final."""
        state = self._mk_state(query)
        friendly = {
            "classify": "[MR Agent] Analyzing your request…",
            "fetch": "[MR Agent] Retrieving information from medical records…",
            "finalize": "[MR Agent] Preparing the response…",
        }
        async for updates in self.graph.astream(
            state,
            stream_mode="updates",
            config={"configurable": {"thread_id": sessionId or str(uuid4())}},
        ):
            for node, data in updates.items():
                if node == "__end__":
                    continue
                if "result" in data:
                    yield {
                        "is_task_complete": True,
                        "require_user_input": False,
                        "content": data["result"],
                    }
                else:
                    yield {
                        "is_task_complete": False,
                        "require_user_input": False,
                        "content": friendly.get(node, "[MR Agent] Working…"),
                    }