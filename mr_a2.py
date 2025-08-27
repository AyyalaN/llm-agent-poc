# medicalrecords_agent.py
# pip install a2a-sdk langgraph uvicorn httpx typing_extensions

import os
import re
import asyncio
import httpx
from typing import TypedDict, Literal, Optional

from langgraph.graph import StateGraph, START, END

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.server.apps.jsonrpc.starlette_app import A2AStarletteApplication
from a2a.server.request_handlers.default_request_handler import DefaultRequestHandler
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.inmemory_push_notification_config_store import InMemoryPushNotificationConfigStore
from a2a.server.tasks.base_push_notification_sender import BasePushNotificationSender

from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.client.card_resolver import A2ACardResolver
from a2a.utils.message import new_agent_text_message, get_message_text
from a2a.types import AgentCard, AgentCapabilities, AgentSkill, AgentInterface, TransportProtocol, Message
try:
    from a2a.types import TextPart
except Exception:  # pragma: no cover
    from a2a.types import Part as TextPart

# --------------------------
# Demo "database"
# --------------------------
RECORDS = {
    "CLM-1001": {
        "diagnoses": ["Hypertension"],
        "procedures": ["Basic metabolic panel"],
        "notes": "BP elevated; lifestyle counseling provided."
    },
    "CLM-1002": {
        "diagnoses": ["Type 2 Diabetes"],
        "procedures": ["A1C test"],
        "notes": "A1C improved; continue metformin."
    },
    "CLM-1003": {
        "diagnoses": ["Migraine"],
        "procedures": ["Neuro eval"],
        "notes": "Trial triptan; follow-up in 4 weeks."
    },
}

class MRState(TypedDict, total=False):
    input: str
    claim_id: Optional[str]
    route: Literal["details", "summary", "delegate_claims"]
    result: str

CLAIM_ID_RE = re.compile(r"(CLM-\d{4})", re.I)

def parse_claim_id(text: str) -> Optional[str]:
    m = CLAIM_ID_RE.search(text or "")
    return m.group(1).upper() if m else None

def route_intent(state: MRState) -> MRState:
    text = (state.get("input") or "").lower()
    claim_id = parse_claim_id(text)
    state["claim_id"] = claim_id

    if any(k in text for k in ["status", "claim details", "what is the claim", "is it approved"]):
        state["route"] = "delegate_claims"
    elif any(k in text for k in ["summary", "summarize", "overview"]):
        state["route"] = "summary"
    else:
        state["route"] = "details"
    return state

def node_details(state: MRState) -> MRState:
    cid = state.get("claim_id")
    rec = RECORDS.get(cid or "", None)
    if not rec:
        state["result"] = "No medical record found."
        return state
    state["result"] = f"Record for {cid}: diagnoses={rec['diagnoses']}, procedures={rec['procedures']}."
    return state

def node_summary(state: MRState) -> MRState:
    cid = state.get("claim_id")
    rec = RECORDS.get(cid or "", None)
    if not rec:
        state["result"] = "No medical record to summarize."
        return state
    state["result"] = (
        f"Summary for {cid}: {', '.join(rec['diagnoses'])}. "
        f"Procedures: {', '.join(rec['procedures'])}. "
        f"Notes: {rec['notes']}"
    )
    return state

async def call_remote_agent(base_url: str, text: str) -> str:
    async with httpx.AsyncClient(timeout=30) as http:
        resolver = A2ACardResolver(http, base_url=base_url)
        card = await resolver.get_agent_card()
        client = ClientFactory(ClientConfig(streaming=True, supported_transports=[TransportProtocol.jsonrpc])).create_client(card)
        msg = Message(role="user", parts=[TextPart(text=text)])
        final_text = ""
        async for event in client.send_message(msg):
            if isinstance(event, tuple):
                continue
            else:
                final_text = get_message_text(event)
        await client.close()
        return final_text or "Peer agent did not return any text."

async def node_delegate_claims(state: MRState) -> MRState:
    claims_url = os.getenv("CLAIMS_AGENT_URL", "http://127.0.0.1:8001")
    cid = state.get("claim_id") or "UNKNOWN"
    prompt = f"Please provide claim details and current status for {cid}."
    state["result"] = await call_remote_agent(claims_url, prompt)
    return state

def build_mr_graph() -> StateGraph:
    g = StateGraph(MRState)
    g.add_node("route_intent", route_intent)
    g.add_node("details", node_details)
    g.add_node("summary", node_summary)
    g.add_node("delegate_claims", node_delegate_claims)
    g.add_edge(START, "route_intent")
    g.add_conditional_edges(
        "route_intent",
        lambda s: s["route"],
        {"details": "details", "summary": "summary", "delegate_claims": "delegate_claims"}
    )
    g.add_edge("details", END)
    g.add_edge("summary", END)
    g.add_edge("delegate_claims", END)
    return g

class MedicalRecordsExecutor(AgentExecutor):
    def __init__(self):
        self.graph = build_mr_graph()

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        user_text = context.get_user_input() or "What can you do?"
        result = await self.graph.ainvoke({"input": user_text})
        final_msg = new_agent_text_message(result["result"], context_id=context.context_id, task_id=context.task_id)
        await updater.complete(final_msg)

MR_CARD = AgentCard(
    name="MedicalRecordsAgent",
    description="Answers questions about medical records and can summarize; can delegate to ClaimsAgent.",
    skills=[
        AgentSkill(id="get_record_details", name="Get Record Details", description="Return medical record facts for a claim ID."),
        AgentSkill(id="summarize_record",  name="Summarize Medical Record", description="Return a brief summary for a claim ID."),
    ],
    capabilities=AgentCapabilities(streaming=True),
    preferred_transport=TransportProtocol.jsonrpc,
)

_task_store = InMemoryTaskStore()
_push_store = InMemoryPushNotificationConfigStore()
_push_sender = BasePushNotificationSender(httpx.AsyncClient(), _push_store)

app = A2AStarletteApplication.build(
    agent_card=MR_CARD,
    request_handler=DefaultRequestHandler(
        agent_executor=MedicalRecordsExecutor(),
        task_store=_task_store,
        push_notification_config_store=_push_store,
        push_notification_sender=_push_sender,
    ),
)