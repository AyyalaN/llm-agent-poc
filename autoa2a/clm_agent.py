# claims_agent.py
import os
import json
import asyncio
from typing import Any, Dict, List, Tuple, Union

import httpx
from a2a.types import AgentCard, AgentCapabilities, AgentSkill, Message, Task, TaskStatusUpdateEvent, TaskArtifactUpdateEvent
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler

from a2a.client.client_factory import ClientFactory, ClientConfig
from a2a.client.card_resolver import A2ACardResolver
from a2a.utils.message import new_agent_text_message

# --- domain stubs ---
_FAKE_DB = {
    "123": {"status": "Paid", "amount": 245.37, "notes": "EOB mailed 2025-03-01"},
    "456": {"status": "Pending review", "amount": 980.00, "notes": "Missing clinical documentation"},
}

def get_claim_details(claim_id: str) -> str:
    d = _FAKE_DB.get(claim_id)
    if not d:
        return f"Claim {claim_id}: not found."
    return f"Claim {claim_id}: status={d['status']}, amount=${d['amount']:.2f}, notes={d['notes']}"

def needs_mr(prompt: str) -> bool:
    p = prompt.lower()
    triggers = ["clinical", "medical record", "medication", "medications", "rx", "labs", "summary"]
    return any(t in p for t in triggers)

# --- Agent Card ---
def build_agent_card(base_url: str) -> AgentCard:
    return AgentCard(
        id="claims-agent",
        name="Claims Agent",
        description="Answers claim status/amounts/EOB. Delegates clinical questions to MR via A2A.",
        version="0.2.0",
        url=base_url,                     # e.g. http://localhost:8001
        capabilities=AgentCapabilities(streaming=True),
        skills=[
            AgentSkill(
                id="get_claim_details",
                name="Get claim details",
                description="Status/amounts/EOB for a claim_id",
                examples=["What's the status/amount for claim 123?"],
                tags=["claims"],
            ),
            AgentSkill(
                id="get_clinical_info",
                name="Get clinical info (via MR)",
                description="If clinical info is requested, delegate to MR",
                examples=["Give me the medical-record summary for claim 456"],
                tags=["delegation", "mr"],
            ),
        ],
    )

# --- helper to delegate to MR and CAPTURE relay events ---
async def delegate_to_mr_and_capture(
    mr_base_url: str,
    prompt: str,
    claim_id: str,
) -> Tuple[str, List[Dict[str, Any]]]:
    relay: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=30.0) as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=mr_base_url)
        mr_card = await resolver.get_agent_card()  # defaults to /.well-known/agent-card.json
        factory = ClientFactory(ClientConfig(streaming=True, httpx_client=httpx_client))
        mr_client = await factory.create(mr_card)

        # log outgoing request
        relay.append({"origin": "claims", "type": "message", "text": f"Delegating to MR: {prompt}", "meta": {"claim_id": claim_id}})
        send_msg: Message = new_agent_text_message(prompt, metadata={"claim_id": claim_id})

        final_text = ""
        async for ev in mr_client.send_message(send_msg):
            # The client yields: (Task, Update) tuples OR final Message
            if isinstance(ev, tuple):
                task_or_update, maybe_update = ev
                # task_or_update is Task
                if isinstance(maybe_update, TaskStatusUpdateEvent):
                    relay.append({"origin": "mr", "type": "status", "state": maybe_update.state})
                elif isinstance(maybe_update, TaskArtifactUpdateEvent):
                    # artifacts often not used here; we keep but compress
                    relay.append({"origin": "mr", "type": "artifact", "name": maybe_update.artifact.name if maybe_update.artifact else None})
                else:
                    # no update (initial task snapshot)
                    relay.append({"origin": "mr", "type": "task", "state": task_or_update.status.state})
            else:
                # Final Message
                if isinstance(ev, Message):
                    # Flatten text parts only for display
                    text_parts = [p.data for p in ev.parts if getattr(p, "data", None) and isinstance(p.data, str)]
                    final_text = "\n".join(text_parts)
                    relay.append({"origin": "mr", "type": "message", "text": final_text})

        await mr_client.close()

    return final_text, relay

# --- Agent Executor ---
class ClaimsAgentExecutor(AgentExecutor):
    def __init__(self):
        self.mr_url = os.getenv("MR_AGENT_BASE_URL", "http://localhost:8002")

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        user_text = (context.message.parts[0].data if context.message and context.message.parts else "").strip()
        claim_id = (context.metadata or {}).get("claim_id") or _guess_claim_id(user_text)

        if not user_text:
            await updater.complete(new_agent_text_message("Please provide a prompt."))
            return

        if not claim_id:
            await updater.complete(new_agent_text_message("Please specify a claim_id (e.g., metadata.claim_id)."))
            return

        if needs_mr(user_text):
            mr_text, relay = await delegate_to_mr_and_capture(self.mr_url, user_text, claim_id)
            out = f"[Claims] Delegated to MR and consolidated the answer:\n{mr_text}"
            # Attach the relay log to the final message metadata so the UI can retrieve it
            await updater.complete(new_agent_text_message(out, metadata={"relay_log": relay}))
        else:
            details = get_claim_details(claim_id)
            await updater.complete(new_agent_text_message(details, metadata={"relay_log": []}))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel(new_agent_text_message("Task cancelled."))

def _guess_claim_id(text: str) -> str | None:
    import re
    m = re.search(r"\b(\d{3,6})\b", text)
    return m.group(1) if m else None

# --- Starlette app factory ---
def create_app() -> "Starlette":
    base_url = os.getenv("CLAIMS_AGENT_BASE_URL", "http://localhost:8001")
    card = build_agent_card(base_url)
    handler = DefaultRequestHandler(
        agent_executor=ClaimsAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )
    app = A2AStarletteApplication(agent_card=card, request_handler=handler)
    return app.build()

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("claims_agent:app", host="0.0.0.0", port=8001, reload=False)