# A2A-compliant Claims Agent (LangGraph StateGraph + hardcoded tools)
# - Collaborates with mr_agent via peer-directed Message (metadata relay hints)
# - Streams status/artifact events and user-facing messages

from __future__ import annotations

import os
from typing import Any, Dict

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.handlers import DefaultRequestHandler
from a2a.server.apps import A2AStarletteApplication
from a2a.server.task_store import InMemoryTaskStore

from a2a.types import (
    AgentCard,
    AgentSkill,
    AgentCapabilities,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TaskArtifactUpdateEvent,
)
from a2a.utils import new_text_artifact, new_agent_text_message

from langgraph.graph import StateGraph, END

# ---------------------------
# Hardcoded claims "data" + tools
# ---------------------------
CLAIMS: Dict[str, Dict[str, Any]] = {
    "C-1001": {
        "status": "Pending Review",
        "provider": "Acme Clinic",
        "service_date": "2025-07-18",
        "billed_amount": 1280.00,
        "allowed_amount": 980.00,
        "materials": {
            "EOB": "EOB: services billed on 2025-07-18, pending adjudication.",
            "Notes": "Provider notes mention follow-up labs in 4 weeks.",
        },
        "mr_ids": ["MR-2001"],
    },
    "C-2002": {
        "status": "Paid",
        "provider": "Metro Health",
        "service_date": "2025-06-02",
        "billed_amount": 420.00,
        "allowed_amount": 315.00,
        "materials": {
            "EOB": "EOB: claim paid at contracted rate.",
            "Notes": "No outstanding actions.",
        },
        "mr_ids": ["MR-2002"],
    },
}

MR_TO_CLAIMS: Dict[str, list[str]] = {
    "MR-2001": ["C-1001"],
    "MR-2002": ["C-2002"],
}

def tool_get_claim_status(claim_id: str) -> str:
    c = CLAIMS.get(claim_id)
    if not c:
        return f"Claim {claim_id} not found."
    return f"Claim {claim_id} status: {c['status']}."

def tool_get_claim_info(claim_id: str) -> str:
    c = CLAIMS.get(claim_id)
    if not c:
        return f"Claim {claim_id} not found."
    return (
        f"Claim {claim_id} at {c['provider']} for {c['service_date']}. "
        f"Billed ${c['billed_amount']:.2f}, allowed ${c['allowed_amount']:.2f}."
    )

def tool_get_material(claim_id: str, name: str) -> str:
    c = CLAIMS.get(claim_id)
    if not c:
        return f"Claim {claim_id} not found."
    mat = c.get("materials", {}).get(name)
    return mat or f"No material '{name}' for claim {claim_id}."

def tool_request_mr_detail(claim_id: str, topic: str) -> str:
    return f"@mr_agent Please provide {topic} for {claim_id}."

def tool_find_claims_by_mr(mr_id: str) -> str:
    ids = MR_TO_CLAIMS.get(mr_id.upper(), [])
    return (f"No claims linked to {mr_id}." if not ids else f"Claims linked to {mr_id}: " + ", ".join(ids))

# ---------------------------
# Minimal LangGraph: route -> exec
# ---------------------------
class ClaimsState(dict):
    """{'prompt': str, 'claim_id': str, 'result': str, 'peer_request': str|None, 'peer_reply': str|None, 'is_peer': bool}"""
    pass

def claims_router(state: ClaimsState) -> str:
    p = state["prompt"].lower()
    # peer queries from MR like: "find claim for MR-xxxx"
    if state.get("is_peer") and "mr-" in p and "claim" in p:
        return "mr_claim_assoc"
    if "status" in p:
        return "status"
    if "info" in p or "details" in p:
        return "info"
    if "eob" in p:
        state["material_name"] = "EOB"
        return "material"
    if "notes" in p:
        state["material_name"] = "Notes"
        return "material"
    # MR topics we don't have locally
    if any(k in p for k in ["medication", "meds", "diagnos", "allerg", "assessment", "summar"]):
        state["mr_topic"] = "medical summary" if "summar" in p else "requested MR details"
        return "mr_collab"
    return "info"

def claims_exec(state: ClaimsState) -> ClaimsState:
    cid = state["claim_id"]
    action = state["action"]
    if action == "status":
        state["result"] = tool_get_claim_status(cid)
    elif action == "info":
        state["result"] = tool_get_claim_info(cid)
    elif action == "material":
        name = state.get("material_name", "EOB")
        state["result"] = tool_get_material(cid, name)
    elif action == "mr_claim_assoc":
        # reverse lookup: MR → Claim(s)
        mr_id = None
        for token in state["prompt"].split():
            if token.upper().startswith("MR-"):
                mr_id = token.upper().rstrip(".,)")
                break
        mr_id = mr_id or "MR-2001"
        summary = tool_find_claims_by_mr(mr_id)
        state["result"] = f"[Claims] {summary}"
        state["peer_reply"] = f"CLAIMS_ASSOC_RESPONSE: {summary}"
    elif action == "mr_collab":
        topic = state.get("mr_topic", "medical details")
        state["result"] = f"Collaborating with MR for {cid}: requesting {topic}."
        state["peer_request"] = tool_request_mr_detail(cid, topic)
    else:
        state["result"] = tool_get_claim_info(cid)
    return state

def build_claims_graph():
    g = StateGraph(ClaimsState)
    g.add_node("exec", claims_exec)

    def decide(state: ClaimsState):
        state["action"] = claims_router(state)
        return "exec"

    g.set_entry_point(decide)
    g.add_edge("exec", END)
    return g.compile()

# ---------------------------
# Agent + Executor
# ---------------------------
class ClaimsAgent:
    def __init__(self) -> None:
        self.graph = build_claims_graph()

    async def run(self, prompt: str, claim_id: str, is_peer: bool = False) -> ClaimsState:
        init = ClaimsState(prompt=prompt, claim_id=claim_id, result="", peer_request=None, peer_reply=None, is_peer=is_peer)
        out = self.graph.invoke(init)
        return out

class ClaimsAgentExecutor(AgentExecutor):
    def __init__(self) -> None:
        self.agent = ClaimsAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Extract text & claim id (+ metadata for peer detection)
        text = ""
        claim_id = ""
        incoming_meta = {}
        try:
            parts = (getattr(context, "message", None) or {}).get("parts", [])
            incoming_meta = (getattr(context, "message", None) or {}).get("metadata", {}) or {}
        except Exception:
            parts = []
        for p in parts or []:
            if p.get("mimeType") == "text/plain" and p.get("text"):
                if not text:
                    text = p["text"]
                if "C-" in p["text"]:
                    claim_id = p["text"].split("C-")[1].split()[0]
                    claim_id = f"C-{claim_id}"
        if not claim_id:
            claim_id = "C-1001"

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                status=TaskStatus(state=TaskState.working, message=f"Processing claim {claim_id}"),
                final=False,
            )
        )

        is_peer = isinstance(incoming_meta, dict) and incoming_meta.get("audience") == "peer"
        result_state = await self.agent.run(text or "info", claim_id, is_peer=is_peer)

        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(artifact=new_text_artifact(result_state["result"]), lastChunk=True)
        )

        # user-facing message (only if not servicing a peer)
        if not is_peer:
            await event_queue.enqueue_event(
                new_agent_text_message(
                    result_state["result"],
                    metadata={"audience": "user", "relay": False},
                )
            )

        # peer-directed requests / replies
        peer_req = result_state.get("peer_request")
        if peer_req:
            await event_queue.enqueue_event(
                new_agent_text_message(
                    peer_req,
                    metadata={
                        "audience": "peer",
                        "relay": True,
                        "target_agent": "mr_agent",
                    },
                )
            )
        peer_reply = result_state.get("peer_reply")
        if is_peer and peer_reply:
            await event_queue.enqueue_event(
                new_agent_text_message(
                    peer_reply,
                    metadata={
                        "audience": "peer",
                        "relay": True,
                        "target_agent": "mr_agent",
                    },
                )
            )

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                status=TaskStatus(state=TaskState.completed, message="Done"),
                final=True,
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception("cancel not supported")

# ---------------------------
# Agent Card + Server (optional runnable)
# ---------------------------
def build_claims_agent_card() -> AgentCard:
    return AgentCard(
        name="claims_agent",
        description="Claims agent: claim status, amounts, materials; collaborates with MR for clinical details.",
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True),
        inputModes=["text/plain"],
        outputModes=["text/plain"],
        skills=[
            AgentSkill(
                id="claims.status",
                name="Get claim status",
                description="Returns the lifecycle status for a claim.",
                examples=["What is the status of C-1001?"],
                tags=["claims", "status"],
            ),
            AgentSkill(
                id="claims.info",
                name="Get claim info",
                description="Returns summary info for a claim (provider, service date, amounts).",
                examples=["Give me details for claim C-2002"],
                tags=["claims", "lookup"],
            ),
            AgentSkill(
                id="claims.materials",
                name="Get claim material",
                description="Returns claim-associated materials (EOB, notes).",
                examples=["Show the EOB for C-1001"],
                tags=["claims", "materials"],
            ),
            AgentSkill(
                id="claims.collab.mr",
                name="Collaborate with MR agent",
                description="Requests MR details when the question is clinical (meds, dx, allergies, assessment, summary).",
                examples=["What medications are on the record for C-1001?"],
                tags=["collaboration", "A2A"],
            ),
        ],
    )

def build_app():
    request_handler = DefaultRequestHandler(
        agent_executor=ClaimsAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )
    return A2AStarletteApplication(
        agent_card=build_claims_agent_card(),
        http_handler=request_handler,
    ).build()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port)