# A2A-compliant Medical Records Agent (LangGraph StateGraph + hardcoded tools)
# - Claim association intent (MR→Claims) + peer delegation
# - Streams TaskStatusUpdateEvent + TaskArtifactUpdateEvent and emits Messages

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
# Hardcoded "data" + tools (keyed by Claim ID)
# ---------------------------
MEDICAL_RECORDS: Dict[str, Dict[str, Any]] = {
    "C-1001": {
        "patient": "John Doe",
        "diagnosis": "Type 2 diabetes mellitus without complications",
        "medications": ["Metformin 500 mg PO BID", "Atorvastatin 20 mg PO QD"],
        "allergies": ["Penicillin"],
        "assessment": "Glycemic control acceptable; continue current regimen.",
        "summary": (
            "Adult male with T2DM; stable vitals; labs within target last visit. "
            "On metformin and atorvastatin; penicillin allergy noted."
        ),
    },
    "C-2002": {
        "patient": "Jane Smith",
        "diagnosis": "Essential hypertension",
        "medications": ["Lisinopril 10 mg PO QD"],
        "allergies": [],
        "assessment": "BP trending down; plan to recheck in 4 weeks.",
        "summary": (
            "Adult female with primary HTN; tolerating lisinopril; "
            "no adverse events reported."
        ),
    },
}

def tool_get_medications(claim_id: str) -> str:
    rec = MEDICAL_RECORDS.get(claim_id)
    if not rec:
        return f"No medical record found for claim {claim_id}."
    meds = rec.get("medications", [])
    return f"Medications for {claim_id}: " + (", ".join(meds) if meds else "None on file.")

def tool_get_diagnosis(claim_id: str) -> str:
    rec = MEDICAL_RECORDS.get(claim_id)
    if not rec:
        return f"No medical record found for claim {claim_id}."
    return f"Diagnosis for {claim_id}: {rec.get('diagnosis', 'N/A')}"

def tool_get_assessment(claim_id: str) -> str:
    rec = MEDICAL_RECORDS.get(claim_id)
    if not rec:
        return f"No medical record found for claim {claim_id}."
    return f"Assessment for {claim_id}: {rec.get('assessment', 'N/A')}"

def tool_get_allergies(claim_id: str) -> str:
    rec = MEDICAL_RECORDS.get(claim_id)
    if not rec:
        return f"No medical record found for claim {claim_id}."
    alg = rec.get("allergies", [])
    return f"Allergies for {claim_id}: " + (", ".join(alg) if alg else "None recorded.")

def tool_summarize_record(claim_id: str) -> str:
    rec = MEDICAL_RECORDS.get(claim_id)
    if not rec:
        return f"No medical record found for claim {claim_id}."
    return f"Summary for {claim_id}: {rec['summary']}"

# ---------------------------
# Minimal LangGraph (router -> tool)
# ---------------------------
class MRState(dict):
    """state: {'prompt': str, 'claim_id': str, 'result': str, 'peer_request': str|None, 'is_peer': bool}"""
    pass

def mr_router(state: MRState) -> str:
    prompt = state["prompt"].lower()
    # Peer response from Claims with association details
    if "claims_assoc_response:" in prompt:
        return "assoc_finalize"
    # User asked claim association from MR side
    if "mr-" in prompt and "claim" in prompt:
        return "assoc_delegate"
    if "medication" in prompt or "meds" in prompt:
        return "medications"
    if "diagnos" in prompt:
        return "diagnosis"
    if "allerg" in prompt:
        return "allergies"
    if "assessment" in prompt:
        return "assessment"
    if "summary" in prompt or "summarize" in prompt:
        return "summary"
    return "summary"

def mr_exec(state: MRState) -> MRState:
    claim_id = state["claim_id"]
    action = state["action"]
    if action == "medications":
        state["result"] = tool_get_medications(claim_id)
    elif action == "diagnosis":
        state["result"] = tool_get_diagnosis(claim_id)
    elif action == "allergies":
        state["result"] = tool_get_allergies(claim_id)
    elif action == "assessment":
        state["result"] = tool_get_assessment(claim_id)
    elif action == "assoc_delegate":
        # Build a peer request for Claims to look up claims for MR id mentioned in prompt
        mr_id = None
        for tok in state["prompt"].split():
            if tok.upper().startswith("MR-"):
                mr_id = tok.upper().rstrip(".,)")
                break
        mr_id = mr_id or "MR-2001"
        state["result"] = f"[MR] Collaborating with Claims to find claim(s) for {mr_id}…"
        state["peer_request"] = f"PLEASE_FIND_CLAIMS_FOR {mr_id}"
    elif action == "assoc_finalize":
        # Turn the peer reply into a friendly user response
        text = state["prompt"].split("CLAIMS_ASSOC_RESPONSE:", 1)[-1].strip()
        state["result"] = f"[MR] {text}"
    else:
        state["result"] = tool_summarize_record(claim_id)
    return state

def build_mr_graph():
    g = StateGraph(MRState)
    g.add_node("exec", mr_exec)

    def decide_next(state: MRState):
        state["action"] = mr_router(state)
        return "exec"

    g.set_entry_point(decide_next)
    g.add_edge("exec", END)
    return g.compile()

# ---------------------------
# Agent + Executor
# ---------------------------
class MRAgent:
    def __init__(self) -> None:
        self.graph = build_mr_graph()

    async def run(self, prompt: str, claim_id: str, is_peer: bool = False) -> MRState:
        init = MRState(prompt=prompt, claim_id=claim_id, action=None, result="", peer_request=None, is_peer=is_peer)
        out = self.graph.invoke(init)
        return out

class MRAgentExecutor(AgentExecutor):
    def __init__(self) -> None:
        self.agent = MRAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Extract user text + metadata
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
                status=TaskStatus(state=TaskState.working, message=f"Fetching MR data for {claim_id}"),
                final=False,
            )
        )

        is_peer = isinstance(incoming_meta, dict) and incoming_meta.get("audience") == "peer"
        result_state = await self.agent.run(text or "summary for claim", claim_id, is_peer=is_peer)

        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(artifact=new_text_artifact(result_state["result"]), lastChunk=True)
        )

        # 1) user-facing message (unless peer-only)
        if not is_peer:
            await event_queue.enqueue_event(
                new_agent_text_message(result_state["result"], metadata={"audience": "user", "relay": False})
            )
        # 2) collaboration request to Claims if needed
        peer_req = result_state.get("peer_request")
        if peer_req:
            await event_queue.enqueue_event(
                new_agent_text_message(
                    peer_req,
                    metadata={"audience": "peer", "relay": True, "target_agent": "claims_agent"},
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
def build_mr_agent_card() -> AgentCard:
    return AgentCard(
        name="mr_agent",
        description="Medical Records agent: medications, diagnosis, allergies, assessments, and record summaries; can collaborate with Claims.",
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True),
        inputModes=["text/plain"],
        outputModes=["text/plain"],
        skills=[
            AgentSkill(
                id="mr.lookup",
                name="Lookup medical record details",
                description="Answer questions about meds/diagnosis/allergies/assessments for a claim.",
                examples=["What medications are in the record for C-1001?"],
                tags=["medical-records", "lookup"],
            ),
            AgentSkill(
                id="mr.summarize",
                name="Summarize a medical record",
                description="Produce a concise medical summary for a claim.",
                examples=["Summarize the medical record for C-1001."],
                tags=["medical-records", "summary"],
            ),
            AgentSkill(
                id="mr.claim_assoc",
                name="Find claims for a medical record",
                description="Collaborate with Claims to find related claims for an MR identifier.",
                examples=["Is there a claim for MR-2001?"],
                tags=["collaboration", "A2A"],
            ),
        ],
    )

def build_app():
    request_handler = DefaultRequestHandler(
        agent_executor=MRAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )
    return A2AStarletteApplication(
        agent_card=build_mr_agent_card(),
        http_handler=request_handler,
    ).build()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10001"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port)