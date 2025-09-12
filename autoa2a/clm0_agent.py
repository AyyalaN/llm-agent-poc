You got it — here’s a drop-in replacement for claims_agent/__main__.py that records every message/event exchanged with the MR Agent, ties everything together with a correlation_id, and (optionally) persists a JSONL audit log per request. The transcript is also returned inline in the final JSON so it’s demo-friendly.

Why this design
• We capture all SSE streaming events (status updates, messages, artifacts) coming back from MR and serialize them to a transcript — this mirrors the A2A streaming model.  ￼
• The MR client is resolved from the MR server’s Agent Card at /.well-known/agent.json, using the SDK’s get_client_from_agent_card_url helper.  ￼

⸻

Updated claims_agent/__main__.py (with full message tracking)

import os
import json
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Dict, Any

from dotenv import load_dotenv
load_dotenv()

# --- AutoGen LLM (OpenAI-compatible) ---
from autogen_ext.models.openai import OpenAIChatCompletionClient

# --- A2A SDK (server + client + types) ---
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
PORT = int(os.getenv("CLAIMS_PORT", "9100"))
MR_BASE_URL = os.getenv("MR_BASE_URL", "http://localhost:9101")
LOG_DIR = os.getenv("LOG_DIR")  # e.g., "./logs"

# --- Sample "DB" ---
CLAIMS_DB = {
    "CLM-1001": {
        "member_id": "M-2003",
        "mr_id": "MR001",
        "diagnosis": "chest pain; hyperlipidemia",
        "claim_amount": 12450.00,
        "narrative": "Hospitalization 2 days; cardiology consult; stress test planned."
    },
    "CLM-1002": {
        "member_id": "M-1118",
        "mr_id": "MR002",
        "diagnosis": "ankle sprain",
        "claim_amount": 350.00,
        "narrative": "ER visit; ankle x-ray negative."
    },
}

# -------------------------
# Transcript/Audit logging
# -------------------------
class Transcript:
    """In-memory + optional JSONL persistence of all events/messages."""
    def __init__(self, correlation_id: str, log_dir: str | None = None):
        self.correlation_id = correlation_id
        self.events: list[dict] = []
        self.path = None
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            self.path = os.path.join(log_dir, f"{correlation_id}.jsonl")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def add(self, actor: str, kind: str, payload: dict):
        evt = {
            "ts": self._now(),
            "correlation_id": self.correlation_id,
            "actor": actor,           # "claims_agent" | "mr_agent" | "user"
            "kind": kind,             # e.g., incoming_request | delegate_start | stream_event | final_decision
            "payload": payload,
        }
        self.events.append(evt)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(evt, ensure_ascii=False) + "\n")

def build_llm() -> OpenAIChatCompletionClient:
    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY", "not-used")
    default_headers = {}
    if os.getenv("BASIC_AUTH"):
        default_headers["Authorization"] = os.getenv("BASIC_AUTH")

    return OpenAIChatCompletionClient(
        model=os.getenv("MODEL_ID", "gpt-4o-mini"),
        base_url=base_url,
        api_key=api_key,
        default_headers=default_headers,
        include_name_in_message=False,
        model_info={"supports_function_calling": False},
        temperature=0.2,
    )

LLM = build_llm()

def _extract_text(message: Message) -> str:
    out = []
    for p in (message.parts or []):
        if isinstance(p, TextPart):
            out.append(p.text)
    return "\n".join(out).strip()

def _serialize_a2a_event(evt: object) -> dict:
    """
    Be robust across SDK versions: inspect common attributes and make a JSON-safe dict.
    Typical streaming events include TaskStatusUpdateEvent, TaskArtifactUpdateEvent, Message.  # noqa
    """
    data: dict[str, Any] = {"event_type": evt.__class__.__name__}

    # Task/status
    status = getattr(evt, "status", None)
    if status is not None:
        # status may be an Enum or string
        data["status"] = getattr(status, "name", status)

    # Task reference
    task = getattr(evt, "task", None)
    if task is not None:
        data["task_id"] = getattr(task, "id", None)

    # Artifact info (if present)
    artifact = getattr(evt, "artifact", None)
    if artifact is not None:
        data["artifact"] = {
            "id": getattr(artifact, "id", None),
            "type": getattr(artifact, "type", None),
            "name": getattr(artifact, "name", None),
        }

    # Message content
    msg = getattr(evt, "message", None)
    if isinstance(msg, Message):
        texts = []
        for part in (msg.parts or []):
            if isinstance(part, TextPart):
                texts.append(part.text)
        data["message_text"] = "\n".join(texts).strip()

    return data

async def _call_mr_agent(
    mr_base_url: str,
    prompt_text: str,
    event_queue: EventQueue,
    transcript: Transcript
) -> Dict[str, Any]:
    """Call MR via A2A streaming; record every event and echo high-level status up the stream."""
    transcript.add("claims_agent", "delegate_start", {"mr_base_url": mr_base_url, "prompt": prompt_text})
    await event_queue.put_status_update(TaskStatus.RUNNING, f"[MR] resolve agent card & connect")  # discovery step

    # Build client from Agent Card (/.well-known/agent.json).  # see docs
    # We avoid relying on contextmanager support for portability.
    mr_client = await A2AClient.get_client_from_agent_card_url(base_url=mr_base_url)
    try:
        req = SendStreamingMessageRequest(
            message=Message(role="user", parts=[TextPart(text=prompt_text)])
        )

        mr_json: Dict[str, Any] = {}
        async for evt in mr_client.send_message_streaming(req):
            # Track raw event for full fidelity
            serialized = _serialize_a2a_event(evt)
            transcript.add("mr_agent", "stream_event", serialized)

            # Bubble a concise status upward for the caller's stream
            if "status" in serialized:
                await event_queue.put_status_update(TaskStatus.RUNNING, f"[MR] {serialized['status']}")
            if "message_text" in serialized and serialized["message_text"]:
                await event_queue.put_status_update(TaskStatus.RUNNING, f"[MR] message chunk received")

            # Parse final MR JSON if/when present
            text = serialized.get("message_text")
            if text:
                try:
                    candidate = json.loads(text)
                    if isinstance(candidate, dict):
                        mr_json = candidate
                except Exception:
                    pass

        transcript.add("claims_agent", "delegate_end", {"mr_json_detected": bool(mr_json)})
        return mr_json
    finally:
        # Ensure underlying HTTP client/session is closed if SDK exposes it
        try:
            await mr_client.aclose()
        except Exception:
            pass

async def evaluate_claim_with_llm(claim: Dict[str, Any], mr_summary: Dict[str, Any]) -> Dict[str, Any]:
    system = """You are a meticulous insurance claims adjudicator.
Given CLAIM and MR_SUMMARY, decide: disposition (APPROVE/DENY/NEEDS_REVIEW),
allowed_amount_estimate (USD), and reasons (3-5 bullets). Output JSON with keys:
disposition, allowed_amount_estimate, reasons."""
    user = f"CLAIM={json.dumps(claim)}\nMR_SUMMARY={json.dumps(mr_summary)}"
    resp = await LLM.create(messages=[
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])
    text = resp.output_text
    try:
        return json.loads(text)
    except Exception:
        return {
            "disposition": "NEEDS_REVIEW",
            "allowed_amount_estimate": claim["claim_amount"] * 0.6,
            "reasons": [text.strip()]
        }

class ClaimsAgentExecutor(AgentExecutor):
    """
    Skill:
      - evaluate_claim: input 'claim_id=CLM-1001' (optionally with extra notes).
        Records full inter-agent transcript and returns it alongside the decision.
    """
    name = "Claims Agent"
    version = "1.1.0"  # bumped for transcript support

    async def on_send_streaming_message(
        self,
        request: SendStreamingMessageRequest,
        task: Task,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> AsyncIterator[SendStreamingMessageResponse]:

        corr_id = str(uuid.uuid4())
        transcript = Transcript(corr_id, LOG_DIR)

        await event_queue.put_status_update(TaskStatus.RUNNING, "Parsing request")
        user_text = _extract_text(request.message)
        transcript.add("user", "incoming_request", {"text": user_text})

        if "claim_id=" not in user_text:
            await event_queue.put_status_update(TaskStatus.FAILED, "Expected 'claim_id=…'")
            transcript.add("claims_agent", "error", {"reason": "missing_claim_id"})
            yield SendStreamingMessageResponse(
                root=JSONRPCErrorResponse(id=request.id, error=UnsupportedOperationError())
            )
            return

        claim_id = user_text.split("claim_id=")[-1].strip().split()[0]
        claim = CLAIMS_DB.get(claim_id)
        if not claim:
            await event_queue.put_status_update(TaskStatus.FAILED, f"Unknown claim {claim_id}")
            transcript.add("claims_agent", "error", {"reason": "unknown_claim", "claim_id": claim_id})
            yield SendStreamingMessageResponse(
                root=JSONRPCErrorResponse(id=request.id, error=UnsupportedOperationError())
            )
            return

        await event_queue.put_status_update(TaskStatus.RUNNING, f"Loaded {claim_id}")
        transcript.add("claims_agent", "claim_loaded", {"claim_id": claim_id, "has_mr": bool(claim.get("mr_id"))})

        mr_summary: Dict[str, Any] = {}
        if claim.get("mr_id"):
            await event_queue.put_status_update(TaskStatus.RUNNING, f"Delegating to MR Agent for {claim['mr_id']}")
            prompt = f"mr_id={claim['mr_id']}"
            mr_summary = await _call_mr_agent(MR_BASE_URL, prompt, event_queue, transcript)

        await event_queue.put_status_update(TaskStatus.RUNNING, "Adjudicating with LLM")
        transcript.add("claims_agent", "adjudication_start", {})

        decision = await evaluate_claim_with_llm(claim, mr_summary)

        transcript.add("claims_agent", "final_decision", {"decision": decision})

        result = {
            "correlation_id": corr_id,
            "claim_id": claim_id,
            "decision": decision,
            "mr_summary": mr_summary,
            "transcript": transcript.events,  # full audit of inter-agent traffic for this request
        }

        # Final message
        yield SendStreamingMessageResponse(
            message=Message(role="assistant", parts=[TextPart(text=json.dumps(result, ensure_ascii=False))])
        )
        await event_queue.put_status_update(TaskStatus.SUCCEEDED, "Completed")

    async def on_cancel(self, request: CancelTaskRequest, task: Task) -> CancelTaskResponse:
        return CancelTaskResponse(
            root=JSONRPCErrorResponse(id=request.id, error=UnsupportedOperationError())
        )

def build_agent_card(base_url: str) -> AgentCard:
    skill = AgentSkill(
        id="evaluate_claim",
        name="Evaluate Claim",
        description="End-to-end claim adjudication, delegates to MR Agent when needed; records full transcript.",
        tags=["claims","adjudication","medical","audit"],
        examples=["claim_id=CLM-1001", "claim_id=CLM-1002"],
    )
    return AgentCard(
        name="Claims Agent",
        description="Evaluates claims, uses MR Agent for clinical summaries, and records an inter-agent transcript.",
        url=base_url,
        version="1.1.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill],
    )

if __name__ == "__main__":
    base_url = f"http://localhost:{PORT}"
    server = A2AServer(
        agent_card=build_agent_card(base_url),
        agent_executor=ClaimsAgentExecutor(),
        request_handler=DefaultA2ARequestHandler(),
    )
    app = server.app
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)


⸻

How this behaves (quick sanity)
	•	When you call claim_id=CLM-1001, the Claims agent resolves the MR’s Agent Card, opens a stream, logs every event coming back (status, messages, artifacts), and writes them to ./logs/<correlation_id>.jsonl if LOG_DIR=./logs is set.  ￼
	•	The final response includes: correlation_id, the adjudication decision, mr_summary, and the full transcript array (so you can render it in a UI or diff across runs).
	•	Event serialization is SDK-version tolerant: we don’t assume concrete class names — we introspect common fields (status, message, artifact, task id) aligned to the A2A streaming model.  ￼

⸻

Optional tweaks
	•	Bubbled streaming: right now we surface concise “[MR] …” statuses to your caller; you can emit richer mid-stream SendStreamingMessageResponse messages if you want to mirror MR message chunks exactly. The underlying transport is SSE with incremental events.  ￼
	•	Storage: swap Transcript to write into MongoDB/Postgres with the same structure (we kept it JSONL for demo speed).
	•	PII controls: redact PHI-like content in _serialize_a2a_event before persisting.

If you want, I can add a tiny /transcripts/<id> endpoint to the Claims server (read-only) and a React log viewer that collapses TaskStatusUpdateEvent vs Message events.