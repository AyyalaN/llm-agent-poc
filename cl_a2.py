# claims_agent.py
# pip install a2a-sdk langgraph uvicorn httpx typing_extensions

import os
import re
import asyncio
import httpx
from typing import TypedDict, Literal, Optional

# --- LangGraph bits ---
from langgraph.graph import StateGraph, START, END

# --- A2A server bits ---
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.server.apps.jsonrpc.starlette_app import A2AStarletteApplication
from a2a.server.request_handlers.default_request_handler import DefaultRequestHandler
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.inmemory_push_notification_config_store import InMemoryPushNotificationConfigStore
from a2a.server.tasks.base_push_notification_sender import BasePushNotificationSender

# --- A2A client bits for delegation ---
from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.client.card_resolver import A2ACardResolver
from a2a.utils.message import new_agent_text_message, get_message_text
from a2a.types import AgentCard, AgentCapabilities, AgentSkill, AgentInterface, TransportProtocol, Message
# TextPart exists in the types module in recent SDKs; fallback below if import path differs
try:
    from a2a.types import TextPart  # >= current releases
except Exception:  # pragma: no cover
    from a2a.types import Part as TextPart  # fallback if SDK aliases text parts

# --------------------------
# Demo "database"
# --------------------------
CLAIMS = {
    "CLM-1001": {"member_id": "M-001", "status": "Pending review", "amount": 1250.00},
    "CLM-1002": {"member_id": "M-002", "status": "Approved", "amount": 340.10},
    "CLM-1003": {"member_id": "M-001", "status": "Denied", "amount": 980.55},
}

# --------------------------
# LangGraph state & helpers
# --------------------------
class ClaimsState(TypedDict, total=False):
    input: str
    claim_id: Optional[str]
    route: Literal["details", "status", "delegate_mr"]
    result: str

CLAIM_ID_RE = re.compile(r"(CLM-\d{4})", re.I)

def parse_claim_id(text: str) -> Optional[str]:
    m = CLAIM_ID_RE.search(text or "")
    return m.group(1).upper() if m else None

def route_intent(state: ClaimsState) -> ClaimsState:
    text = (state.get("input") or "").lower()
    claim_id = parse_claim_id(text)
    state["claim_id"] = claim_id

    if any(k in text for k in ["medical record", "mr", "summary of record", "summarize record", "chart note"]):
        state["route"] = "delegate_mr"
    elif "status" in text or "is it approved" in text:
        state["route"] = "status"
    else:
        state["route"] = "details"
    return state

def node_details(state: ClaimsState) -> ClaimsState:
    cid = state.get("claim_id")
    if not cid or cid not in CLAIMS:
        state["result"] = "Sorry, I couldn’t find that claim."
        return state
    data = CLAIMS[cid]
    state["result"] = f"Claim {cid}: member={data['member_id']}, amount=${data['amount']:.2f}, status={data['status']}."
    return state

def node_status(state: ClaimsState) -> ClaimsState:
    cid = state.get("claim_id")
    if not cid or cid not in CLAIMS:
        state["result"] = "Sorry, I couldn’t find that claim."
        return state
    state["result"] = f"Claim {cid} status: {CLAIMS[cid]['status']}."
    return state

async def call_remote_agent(base_url: str, text: str) -> str:
    async with httpx.AsyncClient(timeout=30) as http:
        # Resolve the peer agent's card
        resolver = A2ACardResolver(http, base_url=base_url)
        card = await resolver.get_agent_card()

        # Build an A2A client (JSON-RPC streaming by default)
        client = ClientFactory(ClientConfig(streaming=True, supported_transports=[TransportProtocol.jsonrpc])).create_client(card)

        # Build a USER message for the peer
        msg = Message(role="user", parts=[TextPart(text=text)])

        # Send and consume the stream; return final text
        final_text = ""
        async for event in client.send_message(msg):
            # The client yields either (Task, UpdateEvent) tuples or a final Message
            if isinstance(event, tuple):
                # We could inspect task/status/artifacts, but for this demo we just keep waiting
                continue
            else:
                final_text = get_message_text(event)  # extract concatenated text from final Message
        await client.close()
        return final_text or "Peer agent did not return any text."

async def node_delegate_mr(state: ClaimsState) -> ClaimsState:
    mr_url = os.getenv("MEDICAL_AGENT_URL", "http://127.0.0.1:8002")
    cid = state.get("claim_id") or "UNKNOWN"
    prompt = f"Please summarize the medical record for claim {cid}. If needed, ask for missing info."
    state["result"] = await call_remote_agent(mr_url, prompt)
    return state

def build_claims_graph() -> StateGraph:
    g = StateGraph(ClaimsState)
    g.add_node("route_intent", route_intent)
    g.add_node("details", node_details)
    g.add_node("status", node_status)
    g.add_node("delegate_mr", node_delegate_mr)
    g.add_edge(START, "route_intent")
    g.add_conditional_edges(
        "route_intent",
        lambda s: s["route"],  # next node name
        {"details": "details", "status": "status", "delegate_mr": "delegate_mr"}
    )
    g.add_edge("details", END)
    g.add_edge("status", END)
    g.add_edge("delegate_mr", END)
    return g

# --------------------------
# A2A AgentExecutor wrapper
# --------------------------
class ClaimsAgentExecutor(AgentExecutor):
    def __init__(self):
        self.graph = build_claims_graph()

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        user_text = context.get_user_input() or "What can you do?"
        result = await self.graph.ainvoke({"input": user_text})
        final_msg = new_agent_text_message(result["result"], context_id=context.context_id, task_id=context.task_id)
        await updater.complete(final_msg)

# --------------------------
# Agent Card
# --------------------------
CLAIMS_CARD = AgentCard(
    name="ClaimsAgent",
    description="Answers questions about health insurance claims; can delegate to MedicalRecordsAgent.",
    skills=[
        AgentSkill(id="get_claim_details", name="Get Claim Details", description="Return details for a claim ID."),
        AgentSkill(id="get_claim_status",  name="Get Claim Status",  description="Return status for a claim ID."),
    ],
    capabilities=AgentCapabilities(streaming=True),
    preferred_transport=TransportProtocol.jsonrpc,
)

# --------------------------
# Build A2A app (Starlette)
# --------------------------
_task_store = InMemoryTaskStore()
_push_store = InMemoryPushNotificationConfigStore()
_push_sender = BasePushNotificationSender(httpx.AsyncClient(), _push_store)

app = A2AStarletteApplication.build(
    agent_card=CLAIMS_CARD,
    request_handler=DefaultRequestHandler(
        agent_executor=ClaimsAgentExecutor(),
        task_store=_task_store,
        push_notification_config_store=_push_store,
        push_notification_sender=_push_sender,
    ),
)