# medicalrecords_agent.py
import asyncio
from typing import TypedDict, Optional
from uuid import uuid4

import uvicorn
import httpx

from langgraph.graph import StateGraph, START, END

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue

from a2a.client import A2ACardResolver, A2AClient
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Message,
    MessageSendParams,
    SendMessageRequest,
    Part,
    TextPart,
)
from a2a.utils import new_agent_text_message, get_message_text


# --------------------------
# Demo data (hardcoded)
# --------------------------
MEDICAL_DB = {
    "M-001": [
        {"date": "2025-05-01", "note": "BP elevated; start lisinopril 10mg."},
        {"date": "2025-06-11", "note": "Normal EKG. Follow-up in 6 months."},
    ],
    "M-002": [
        {"date": "2025-04-14", "note": "A1C 7.1%; continue metformin."},
        {"date": "2025-05-23", "note": "Comprehensive metabolic panel within normal limits."},
    ],
}

CLAIMS_AGENT_URL = "http://localhost:9101"   # target for delegation


# --------------------------
# LangGraph State
# --------------------------
class MedState(TypedDict, total=False):
    input: str
    member_id: Optional[str]
    result: Optional[str]


# --------------------------
# Delegation utility
# --------------------------
async def delegate_to_claims(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=30) as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=CLAIMS_AGENT_URL)
        public_card = await resolver.get_public_agent_card()
        client = A2AClient(httpx_client=httpx_client, agent_card=public_card)
        message = Message(
            role="user",
            parts=[Part(root=TextPart(text=prompt))],
            messageId=uuid4().hex,
        )
        req = SendMessageRequest(id=str(uuid4()), params=MessageSendParams(message=message))
        resp = await client.send_message(req)
        rd = resp.model_dump(mode="json", exclude_none=True)
        parts = (
            rd.get("result", {}).get("parts")
            or rd.get("result", {}).get("message", {}).get("parts")
            or []
        )
        for p in parts:
            if (p.get("type") or p.get("kind")) == "text":
                return p.get("text", "")
        return "Delegation succeeded, but no textual response was found."


# --------------------------
# Helpers
# --------------------------
def guess_member_id(text: str) -> Optional[str]:
    import re
    m = re.search(r"(M-\d+)", text.upper())
    return m.group(1) if m else None

def route(state: MedState) -> str:
    t = (state.get("input") or "").lower()
    if "claim" in t or "status" in t:
        return "delegate_to_claims"
    return "summarize_records"

async def summarize_records(state: MedState) -> MedState:
    t = state["input"]
    member = guess_member_id(t) or state.get("member_id")
    if not member or member not in MEDICAL_DB:
        return {"result": "Please provide a valid member id like M-001."}
    items = MEDICAL_DB[member][-3:]  # latest up to 3
    lines = [f"- {it['date']}: {it['note']}" for it in items]
    return {"result": f"Medical summary for {member}:\n" + "\n".join(lines)}

async def delegate_to_claims_node(state: MedState) -> MedState:
    t = state["input"]
    member = guess_member_id(t)
    prefix = f"For member {member}, " if member else ""
    delegated = await delegate_to_claims(f"{prefix}what's the status of the related claims?")
    return {"result": f"(Delegated to claims) {delegated}"}


# --------------------------
# Compile LangGraph
# --------------------------
def build_graph():
    g = StateGraph(MedState)
    g.add_node("summarize_records", summarize_records)
    g.add_node("delegate_to_claims", delegate_to_claims_node)
    g.add_node("router", lambda s: s)
    g.add_edge(START, "router")
    g.add_conditional_edges("router", route, {"summarize_records": "summarize_records", "delegate_to_claims": "delegate_to_claims"})
    g.add_edge("summarize_records", END)
    g.add_edge("delegate_to_claims", END)
    return g.compile()


# --------------------------
# A2A Executor
# --------------------------
class MedicalRecordsExecutor(AgentExecutor):
    def __init__(self):
        self.app = build_graph()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = get_message_text(context.message) or ""
        out = await self.app.ainvoke({"input": user_text})
        final_text = out.get("result") or "No result."
        event_queue.enqueue_event(new_agent_text_message(final_text))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception("cancel not supported")


# --------------------------
# Boot the server
# --------------------------
if __name__ == "__main__":
    skill = AgentSkill(
        id="medical-records",
        name="Medical Records",
        description="Summarize and fetch medical records; can delegate to claims.",
        tags=["medical", "records", "summary"],
        examples=["summarize records for M-001", "claim status for M-002", "medical record summary"],
    )
    public_card = AgentCard(
        name="medical-records-agent",
        description="Medical records agent (demo) that can also delegate to claims.",
        url="http://localhost:9102/",
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill],
    )

    handler = DefaultRequestHandler(
        agent_executor=MedicalRecordsExecutor(),
        task_store=InMemoryTaskStore(),
    )
    server = A2AStarletteApplication(
        agent_card=public_card,
        http_handler=handler,
    )
    uvicorn.run(server.build(), host="0.0.0.0", port=9102)