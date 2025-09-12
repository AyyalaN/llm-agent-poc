# claims_agent.py
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
CLAIMS_DB = {
    "CLM-1001": {
        "member_id": "M-001",
        "status": "Pending review",
        "amount": 250.0,
        "date": "2025-06-10",
        "diagnosis_code": "I10",
        "procedure_code": "93000",
    },
    "CLM-2002": {
        "member_id": "M-002",
        "status": "Approved",
        "amount": 1320.5,
        "date": "2025-05-22",
        "diagnosis_code": "E11.9",
        "procedure_code": "80050",
    },
}

MED_AGENT_URL = "http://localhost:9102"   # target for delegation


# --------------------------
# LangGraph State
# --------------------------
class ClaimsState(TypedDict, total=False):
    input: str
    claim_id: Optional[str]
    result: Optional[str]


# --------------------------
# Delegation utility
# --------------------------
async def delegate_to_med_agent(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=30) as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=MED_AGENT_URL)
        public_card = await resolver.get_public_agent_card()

        client = A2AClient(httpx_client=httpx_client, agent_card=public_card)

        message = Message(
            role="user",
            parts=[Part(root=TextPart(text=prompt))],
            messageId=uuid4().hex,
        )
        req = SendMessageRequest(
            id=str(uuid4()), params=MessageSendParams(message=message)
        )
        resp = await client.send_message(req)
        # The success payload's `result` is a Message in this simple example
        result_dict = resp.model_dump(mode="json", exclude_none=True)
        # Try common locations for the agent's text
        parts = (
            result_dict.get("result", {})
            .get("parts")
            or result_dict.get("result", {})
            .get("message", {})
            .get("parts")
            or []
        )
        for p in parts:
            if (p.get("type") or p.get("kind")) == "text":
                return p.get("text", "")
        return "Delegation succeeded, but no textual response was found."
        

# --------------------------
# Nodes
# --------------------------
def route(state: ClaimsState) -> str:
    text = (state.get("input") or "").lower()
    if "summary" in text or "record" in text or "medical" in text:
        return "delegate_to_med"
    return "handle_claim"

def extract_claim_id(text: str) -> Optional[str]:
    # naive parse "CLM-xxxx"
    import re
    m = re.search(r"(CLM-\d+)", text.upper())
    return m.group(1) if m else None

async def handle_claim(state: ClaimsState) -> ClaimsState:
    text = state["input"]
    claim_id = extract_claim_id(text) or state.get("claim_id")
    if not claim_id or claim_id not in CLAIMS_DB:
        return {"result": "Please provide a valid claim id like CLM-1001."}
    claim = CLAIMS_DB[claim_id]
    return {
        "result": (
            f"Claim {claim_id}: status={claim['status']}, amount=${claim['amount']}, "
            f"date={claim['date']}, dx={claim['diagnosis_code']}, px={claim['procedure_code']}."
        )
    }

async def delegate_to_med(state: ClaimsState) -> ClaimsState:
    text = state["input"]
    claim_id = extract_claim_id(text)
    prefix = f"For member linked to {claim_id}, " if claim_id else ""
    delegated = await delegate_to_med_agent(
        f"{prefix}please summarize the relevant recent medical records."
    )
    return {"result": f"(Delegated to medical-records) {delegated}"}


# --------------------------
# Compile LangGraph
# --------------------------
def build_graph():
    g = StateGraph(ClaimsState)
    g.add_node("handle_claim", handle_claim)
    g.add_node("delegate_to_med", delegate_to_med)
    g.add_edge(START, "router")
    g.add_conditional_edges("router", route, {"handle_claim": "handle_claim", "delegate_to_med": "delegate_to_med"})
    g.add_edge("handle_claim", END)
    g.add_edge("delegate_to_med", END)
    g.add_node("router", lambda s: s)  # simple pass-through for conditional start
    return g.compile()


# --------------------------
# A2A Executor
# --------------------------
class ClaimsAgentExecutor(AgentExecutor):
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
        id="claims",
        name="Claims Info & Status",
        description="Lookup claims and status; can delegate to medical-records for summaries.",
        tags=["claims", "status", "healthcare"],
        examples=["status of CLM-1001", "give details for CLM-2002", "summarize medical records for CLM-1001"],
    )
    public_card = AgentCard(
        name="claims-agent",
        description="Claims agent (demo) that can also delegate to medical-records.",
        url="http://localhost:9101/",
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill],
    )

    handler = DefaultRequestHandler(
        agent_executor=ClaimsAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )
    server = A2AStarletteApplication(
        agent_card=public_card,
        http_handler=handler,
    )
    uvicorn.run(server.build(), host="0.0.0.0", port=9101)