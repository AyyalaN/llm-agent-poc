# claims_agent.py
# A LangGraph stategraph agent specialized for Claims (status/info/materials).
# Compatible with the Google A2A sample agent_executor.py expectations.
from __future__ import annotations

import re
from typing import Any, AsyncGenerator, Annotated, TypedDict, Optional
from uuid import uuid4

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langchain_core.messages import HumanMessage, AIMessage


class ClaimsState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    intent: str
    params: dict
    result: str


# ---- Hardcoded "tools" with in-memory dicts ----

# minimal claim corpus
_CLAIMS: dict[str, dict[str, Any]] = {
    "C-1001": {
        "member_id": "M-7788",
        "status": "Pending review",
        "amount": 1200.55,
        "service_date": "2025-07-05",
        "provider": "Downtown Imaging Center",
        "mr_ids": ["MR-2001"],
        "notes": "Awaiting medical records from provider.",
    },
    "C-1002": {
        "member_id": "M-9911",
        "status": "Approved",
        "amount": 245.75,
        "service_date": "2025-06-14",
        "provider": "Green Valley Clinic",
        "mr_ids": ["MR-2002"],
        "notes": "Paid on 2025-07-01.",
    },
}

# claim-linked "materials" (pretend attachments list)
_CLAIM_MATERIALS: dict[str, list[str]] = {
    "C-1001": ["MR-2001", "IMG-CT-734", "FAX-REQ-0821"],
    "C-1002": ["MR-2002", "PDF-EOB-1002"],
}


def _get_claim_id(text: str) -> Optional[str]:
    m = re.search(r"\bC-\d{4}\b", text, flags=re.I)
    return m.group(0).upper() if m else None


def tool_get_claim_status(claim_id: str) -> str:
    c = _CLAIMS.get(claim_id)
    if not c:
        return f"Claim {claim_id} not found."
    return f"Claim {claim_id} status: {c['status']}"


def tool_get_claim_info(claim_id: str, field: str) -> str:
    c = _CLAIMS.get(claim_id)
    if not c:
        return f"Claim {claim_id} not found."
    if field not in c:
        return f"Field '{field}' not found on claim {claim_id}."
    return f"{field.replace('_', ' ').title()} for {claim_id}: {c[field]}"


def tool_get_claim_materials(claim_id: str) -> str:
    items = _CLAIM_MATERIALS.get(claim_id, [])
    if not items:
        return f"No materials found for claim {claim_id}."
    return f"Materials for {claim_id}: " + ", ".join(items)


# ---- Node functions ----

def classify(state: ClaimsState) -> ClaimsState:
    """Tiny rule-based router: status / info / materials."""
    text = state["messages"][-1].content.lower()
    claim_id = _get_claim_id(text) or "C-1001"  # default demo ID
    # Choose intent/field
    if "status" in text:
        intent, field = "status", ""
    elif any(k in text for k in ("material", "attachment", "document", "docs")):
        intent, field = "materials", ""
    else:
        # heuristic field detection
        if any(k in text for k in ("amount", "billed", "paid")):
            intent, field = "info", "amount"
        elif any(k in text for k in ("service date", "dos", "date of service")):
            intent, field = "info", "service_date"
        elif "provider" in text:
            intent, field = "info", "provider"
        elif any(k in text for k in ("mr", "medical record")):
            # NOTE: Claims agent *doesn't* have record access;
            # this surfaces a hint to collaborate via A2A.
            intent, field = "handoff_hint", ""
        else:
            intent, field = "info", "notes"

    return {"intent": intent, "params": {"claim_id": claim_id, "field": field}}


def fetch(state: ClaimsState) -> ClaimsState:
    intent = state["intent"]
    claim_id = state["params"]["claim_id"]
    field = state["params"]["field"]

    if intent == "status":
        msg = tool_get_claim_status(claim_id)
    elif intent == "materials":
        msg = tool_get_claim_materials(claim_id)
    elif intent == "info":
        msg = tool_get_claim_info(claim_id, field)
    else:
        # handoff hint: tell the orchestrator/user which MR to consult
        mr_ids = _CLAIMS.get(claim_id, {}).get("mr_ids", [])
        if mr_ids:
            msg = (
                f"[Claims Agent] I don’t have medical-record access. "
                f"Please consult the MR agent for {', '.join(mr_ids)}."
            )
        else:
            msg = "[Claims Agent] No linked medical record IDs found."

    prefix = "[Claims Agent] "
    return {"result": prefix + msg}


def finalize(state: ClaimsState) -> ClaimsState:
    out = state["result"]
    return {"messages": [AIMessage(content=out)], "result": out}


# ---- Agent class ----

class ClaimsAgent:
    """Claims agent (LangGraph stategraph).
    Provides:
      - invoke(query, sessionId=None) -> dict
      - stream(query, sessionId=None) -> async generator of dicts
    """
    SUPPORTED_CONTENT_TYPES = ["text/plain"]

    def __init__(self) -> None:
        builder = StateGraph(ClaimsState)
        builder.add_node("classify", classify)
        builder.add_node("fetch", fetch)
        builder.add_node("finalize", finalize)

        def router(s: ClaimsState) -> str:
            return "fetch"

        builder.set_entry_point("classify")
        builder.add_conditional_edges("classify", router, {"fetch": "fetch"})
        builder.add_edge("fetch", "finalize")
        builder.add_edge("finalize", END)

        self.graph = builder.compile()

    def _mk_state(self, query: str) -> ClaimsState:
        return {"messages": [HumanMessage(content=query)]}

    def invoke(self, query: str, sessionId: str | None = None) -> dict:
        result = self.graph.invoke(
            self._mk_state(query),
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
        friendly = {
            "classify": "[Claims Agent] Understanding your request…",
            "fetch": "[Claims Agent] Retrieving claim information…",
            "finalize": "[Claims Agent] Preparing the response…",
        }
        async for updates in self.graph.astream(
            self._mk_state(query),
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
                        "content": friendly.get(node, "[Claims Agent] Working…"),
                    }