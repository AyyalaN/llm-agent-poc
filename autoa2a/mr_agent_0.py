import os
import json
import asyncio
from typing import AsyncIterator, Dict, Any

from dotenv import load_dotenv
load_dotenv()

# --- AutoGen (OpenAI-compatible LLM) ---
from autogen_ext.models.openai import OpenAIChatCompletionClient
# See docs for base_url/model_info/default_headers knobs.  [oai_citation:4‡Microsoft GitHub](https://microsoft.github.io/autogen/stable//user-guide/agentchat-user-guide/migration-guide.html?utm_source=chatgpt.com)

# --- A2A SDK (server + types + client) ---
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue, TaskStatus
from a2a.server import A2AServer, DefaultA2ARequestHandler
from a2a.client import A2AClient
from a2a.types import (
    AgentCard, AgentCapabilities, AgentSkill,
    Message, TextPart, Task,
    SendStreamingMessageRequest, SendStreamingMessageResponse,
    JSONRPCErrorResponse, UnsupportedOperationError,
    CancelTaskRequest, CancelTaskResponse,
)

HOST = "0.0.0.0"
PORT = int(os.getenv("MR_PORT", "9101"))

# --- Sample "DB" ---
MR_DB = {
    "MR001": {
        "patient": {"name": "A. Patel", "dob": "1979-04-11"},
        "notes": """Chief complaint: chest pain on exertion. History: HTN, smoker.
Meds: lisinopril 10mg. Labs: LDL 181 mg/dL. ECG: nonspecific ST-T changes.
Family Hx: father MI at 52. Plan: exercise stress test; start statin."""
    },
    "MR002": {
        "patient": {"name": "J. Romero", "dob": "1988-10-02"},
        "notes": """ER visit for ankle sprain. No fracture on X-ray. RICE advised."""
    },
}

def build_llm() -> OpenAIChatCompletionClient:
    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY", "not-used")
    default_headers = {}
    if os.getenv("BASIC_AUTH"):
        default_headers["Authorization"] = os.getenv("BASIC_AUTH")

    # Minimal, provider-agnostic client (OpenAI-compatible).  [oai_citation:5‡Microsoft GitHub](https://microsoft.github.io/autogen/stable//user-guide/agentchat-user-guide/migration-guide.html?utm_source=chatgpt.com)
    return OpenAIChatCompletionClient(
        model=os.getenv("MODEL_ID", "gpt-4o-mini"),
        base_url=base_url,
        api_key=api_key,
        default_headers=default_headers,
        # Some OpenAI-compatible backends bar the 'name' field & function calling.
        include_name_in_message=False,   # toggle per provider  [oai_citation:6‡Microsoft GitHub](https://microsoft.github.io/autogen/dev//reference/python/autogen_ext.models.openai.config.html?utm_source=chatgpt.com)
        model_info={"supports_function_calling": False},
        temperature=0.2,
    )

LLM = build_llm()

def _extract_text(message: Message) -> str:
    out = []
    for p in message.parts or []:
        if isinstance(p, TextPart):
            out.append(p.text)
    return "\n".join(out).strip()

async def summarize_record_text(text: str) -> Dict[str, Any]:
    # One-shot prompt—AutoGen client wraps the OpenAI Responses API.
    prompt = f"""
You are a clinical summarizer. Read the medical text and synthesize:
- SUMMARY (3-5 bullet points)
- ICD10_SUSPECTS (codes or descriptions; conservative guesses ok)
- RED_FLAGS (explicit warning signs, if any)
- RECOMMENDATIONS (next diagnostic/tx steps)
Return valid JSON keys: summary, icd10_suspects, red_flags, recommendations.

TEXT:
{text}
"""
    resp = await LLM.create(messages=[{"role":"user", "content": prompt}])
    content = resp.output_text  # unified content string
    # Be permissive: attempt JSON parse; on fail, wrap as summary.
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        for k in ["summary","icd10_suspects","red_flags","recommendations"]:
            data.setdefault(k, [])
        return data
    except Exception:
        return {
            "summary": [content.strip()],
            "icd10_suspects": [],
            "red_flags": [],
            "recommendations": []
        }

class MRAgentExecutor(AgentExecutor):
    """
    Skills:
      - summarize_mr: Accepts 'mr_id' or free text; returns structured JSON.
    """

    name = "MR Agent (Summarization)"
    version = "1.0.0"

    async def on_send_streaming_message(
        self,
        request: SendStreamingMessageRequest,
        task: Task,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> AsyncIterator[SendStreamingMessageResponse]:
        # Status: started
        await event_queue.put_status_update(TaskStatus.RUNNING, "Parsing request")

        user_text = _extract_text(request.message)
        target_id = None
        raw_text = None

        # lightweight parsing: expect either "mr_id=MR001" or raw text
        if "mr_id=" in user_text:
            target_id = user_text.split("mr_id=")[-1].strip().split()[0]
            await event_queue.put_status_update(TaskStatus.RUNNING, f"Loading MR {target_id}")
            rec = MR_DB.get(target_id)
            if not rec:
                await event_queue.put_status_update(TaskStatus.FAILED, f"Unknown MR: {target_id}")
                yield SendStreamingMessageResponse(
                    root=JSONRPCErrorResponse(id=request.id, error=UnsupportedOperationError())
                )
                return
            raw_text = rec["notes"]
        else:
            raw_text = user_text

        await event_queue.put_status_update(TaskStatus.RUNNING, "Summarizing…")
        data = await summarize_record_text(raw_text)

        # Final message (as JSON string)
        final_text = json.dumps(data, ensure_ascii=False)
        yield SendStreamingMessageResponse(
            message=Message(role="assistant", parts=[TextPart(text=final_text)])
        )
        await event_queue.put_status_update(TaskStatus.SUCCEEDED, "Done")

    # (Optional) Reject unsupported operations to be explicit.
    async def on_cancel(self, request: CancelTaskRequest, task: Task) -> CancelTaskResponse:
        return CancelTaskResponse(
            root=JSONRPCErrorResponse(id=request.id, error=UnsupportedOperationError())
        )

def build_agent_card(base_url: str) -> AgentCard:
    skill = AgentSkill(
        id="summarize_mr",
        name="Summarize Medical Record",
        description="Summarize a medical record and flag potential issues.",
        tags=["medical", "summary", "icd10", "risk"],
        examples=[
            'mr_id=MR001',
            'Patient with chest pain and high LDL… (paste text)'
        ],
    )
    return AgentCard(
        name="MR Agent",
        description="Summarizes medical records into structured insights.",
        url=base_url,
        version="1.0.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill],
    )

if __name__ == "__main__":
    base_url = f"http://localhost:{PORT}"
    server = A2AServer(
        agent_card=build_agent_card(base_url),
        agent_executor=MRAgentExecutor(),
        request_handler=DefaultA2ARequestHandler(),
    )
    # Expose Starlette app for uvicorn import path
    app = server.app
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)