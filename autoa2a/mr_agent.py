# mr_agent.py
import os
from typing import Any, Dict, List

from a2a.types import AgentCard, AgentCapabilities, AgentSkill
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.utils.message import new_agent_text_message

# --- domain stub ---
def get_clinical_summary(claim_id: str) -> str:
    return (
        f"Clinical summary for claim {claim_id}:\n"
        "- Dx: Acute sinusitis\n"
        "- Meds: amoxicillin 500mg TID x7d\n"
        "- Allergies: NKDA\n"
        "- Notes: No red flags; follow-up PRN"
    )

def get_medications(claim_id: str) -> str:
    return f"Medications for claim {claim_id}: amoxicillin 500mg TID (7 days)."

# --- A2A Agent Card ---
def build_agent_card(base_url: str) -> AgentCard:
    return AgentCard(
        id="mr-agent",
        name="Medical Records Agent",
        description="Provides clinical details (summaries, medications) tied to claims.",
        version="0.1.0",
        url=base_url,                     # e.g. http://localhost:8002
        capabilities=AgentCapabilities(streaming=True),
        skills=[
            AgentSkill(
                id="get_clinical_info",
                name="Get clinical info for a claim",
                description="Summaries & medications for a claim_id",
                examples=[
                    "Provide a medical-record summary for claim 123",
                    "List medications for claim 456"
                ],
                tags=["clinical", "mr", "claims"],
            )
        ],
    )

# --- Agent Executor ---
class MRAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        # very simple routing based on text
        user_text = (context.message.parts[0].data if context.message and context.message.parts else "").lower()
        claim_id = context.metadata.get("claim_id") if context.metadata else None

        if not claim_id:
            await updater.complete(new_agent_text_message("Please include claim_id in metadata."))
            return

        if "med" in user_text or "medication" in user_text or "drug" in user_text:
            resp = get_medications(claim_id)
        else:
            resp = get_clinical_summary(claim_id)

        await updater.complete(new_agent_text_message(resp))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel(new_agent_text_message("Task cancelled."))

# --- Starlette app factory ---
def create_app() -> "Starlette":
    base_url = os.getenv("MR_AGENT_BASE_URL", "http://localhost:8002")
    card = build_agent_card(base_url)
    handler = DefaultRequestHandler(
        agent_executor=MRAgentExecutor(),
        task_store=InMemoryTaskStore(),
    )
    app = A2AStarletteApplication(agent_card=card, request_handler=handler)
    return app.build()

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mr_agent:app", host="0.0.0.0", port=8002, reload=False)