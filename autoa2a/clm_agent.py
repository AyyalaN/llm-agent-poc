import os
import json
import uuid
import threading
from datetime import datetime, timezone
from typing import AsyncIterator, Dict, Any, List, Tuple

from dotenv import load_dotenv
load_dotenv()

# ==== LLM (AutoGen, OpenAI-compatible backends like vLLM/TGI) ====
from autogen_ext.models.openai import OpenAIChatCompletionClient  # docs: include_name_in_message, base_url

# ==== A2A SDK (server + client) ====
# NOTE: If your installed a2a-sdk exposes slightly different module paths or names,
# adjust these imports accordingly.
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

import uvicorn
import gradio as gr

# --------------------------------------------------------------------------------------
# ENV / CONFIG
# --------------------------------------------------------------------------------------
HOST = "0.0.0.0"
CLAIMS_PORT = int(os.getenv("CLAIMS_PORT", "9100"))         # A2A server (Claims)
GRADIO_PORT = int(os.getenv("GRADIO_PORT", "7860"))         # Gradio UI
MR_BASE_URL = os.getenv("MR_BASE_URL", "http://localhost:9101")
LOG_DIR = os.getenv("LOG_DIR")  # e.g., "./logs"

# THEME CONFIG (edit these to re-skin)
COLORS = {
    "bg": "#0f1117",
    "panel": "#151922",
    "text": "#e6e6e6",
    "accent": "#6ea8fe",
    "user": "#9aa0a6",
    "claims": "#66b2ff",
    "mr": "#6fe0a8",
    "error": "#ff6b6b",
    "status": "#bdbdbd",
}
FONTS = {"mono": "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace"}

# --------------------------------------------------------------------------------------
# SAMPLE DATA (enriched with admin-only fields)
# --------------------------------------------------------------------------------------
CLAIMS_DB = {
    "CLM-1001": {
        "member_id": "M-2003",
        "mr_id": "MR001",
        "diagnosis": "chest pain; hyperlipidemia",
        "claim_amount": 12450.00,
        "claim_status": "In Review",              # NEW
        "eob_notes": "Pending cardiology review", # NEW
        "narrative": "Hospitalization 2 days; cardiology consult; stress test planned."
    },
    "CLM-1002": {
        "member_id": "M-1118",
        "mr_id": "MR002",
        "diagnosis": "ankle sprain",
        "claim_amount": 350.00,
        "claim_status": "Approved",               # NEW
        "eob_notes": "Standard ER visit",         # NEW
        "narrative": "ER visit; ankle x-ray negative."
    },
}

# --------------------------------------------------------------------------------------
# CLINICAL-INTENT GATE
# --------------------------------------------------------------------------------------
CLINICAL_KEYWORDS = {
    "clinical", "summary", "summarize", "mr", "medical record", "medications",
    "meds", "allergies", "icd10", "icd-10", "diagnosis", "labs", "vitals",
    "red flags", "risk", "imaging", "radiology"
}
def needs_clinical(user_text: str) -> bool:
    q = user_text.lower()
    return any(k in q for k in CLINICAL_KEYWORDS)

# --------------------------------------------------------------------------------------
# TRANSCRIPT / AUDIT
# --------------------------------------------------------------------------------------
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

# --------------------------------------------------------------------------------------
# LLM (AutoGen, OpenAI-compatible)
# --------------------------------------------------------------------------------------
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
        include_name_in_message=False,               # toggle for strict OpenAI-compatible servers
        model_info={"supports_function_calling": False},
        temperature=0.2,
    )

LLM = build_llm()

# --------------------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------------------
def _extract_text(message: Message) -> str:
    out = []
    for p in (message.parts or []):
        if isinstance(p, TextPart):
            out.append(p.text)
    return "\n".join(out).strip()

def _serialize_a2a_event(evt: object) -> dict:
    """SDK-version tolerant event normalization for human-readable rendering."""
    data: dict[str, Any] = {"event_type": evt.__class__.__name__}

    status = getattr(evt, "status", None)
    if status is not None:
        data["status"] = getattr(status, "name", status)

    task = getattr(evt, "task", None)
    if task is not None:
        data["task_id"] = getattr(task, "id", None)

    artifact = getattr(evt, "artifact", None)
    if artifact is not None:
        data["artifact"] = {
            "id": getattr(artifact, "id", None),
            "type": getattr(artifact, "type", None),
            "name": getattr(artifact, "name", None),
        }

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
    """Call MR via A2A streaming; record every event and bubble concise statuses."""
    transcript.add("claims_agent", "delegate_start", {"mr_base_url": mr_base_url, "prompt": prompt_text})
    await event_queue.put_status_update(TaskStatus.RUNNING, f"[MR] resolving agent card")

    # Build A2A client from the collaborator's Agent Card (/.well-known/agent.json)
    mr_client = await A2AClient.get_client_from_agent_card_url(base_url=mr_base_url)
    try:
        req = SendStreamingMessageRequest(
            message=Message(role="user", parts=[TextPart(text=prompt_text)])
        )

        mr_json: Dict[str, Any] = {}
        async for evt in mr_client.send_message_streaming(req):
            serialized = _serialize_a2a_event(evt)
            transcript.add("mr_agent", "stream_event", serialized)

            # Human-friendly status for outer stream
            if "status" in serialized:
                await event_queue.put_status_update(TaskStatus.RUNNING, f"[MR] {serialized['status']}")
            if "message_text" in serialized and serialized["message_text"]:
                await event_queue.put_status_update(TaskStatus.RUNNING, f"[MR] message chunk")

            # Try to parse final MR JSON
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
    resp = await LLM.create(messages=[{"role":"system","content":system},{"role":"user","content":user}])
    text = resp.output_text
    try:
        return json.loads(text)
    except Exception:
        return {
            "disposition": "NEEDS_REVIEW",
            "allowed_amount_estimate": claim["claim_amount"] * 0.6,
            "reasons": [text.strip()]
        }

def build_admin_info(claim: Dict[str, Any]) -> Dict[str, Any]:
    """Local (non-clinical) answer constructed from Claims DB only."""
    return {
        "claim_status": claim.get("claim_status"),
        "claim_amount": claim.get("claim_amount"),
        "eob_notes": claim.get("eob_notes"),
        "member_id": claim.get("member_id"),
        "mr_linked": bool(claim.get("mr_id")),
    }

# --------------------------------------------------------------------------------------
# CLAIMS A2A EXECUTOR (with clinical-intent gating)
# --------------------------------------------------------------------------------------
class ClaimsAgentExecutor(AgentExecutor):
    """
    Skill:
      - evaluate_claim: 'claim_id=CLM-1001' (optional natural language).
        - If the prompt is clinical -> delegate to MR via A2A.
        - Otherwise answer locally with admin info (no MR calls).
        Returns transcript for UI relay.
    """
    name = "Claims Agent"
    version = "1.3.0"  # gating + admin-info + UI support

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
        is_clinical = needs_clinical(user_text)
        will_delegate = claim.get("mr_id") and is_clinical
        transcript.add("claims_agent", "routing_decision", {"will_delegate": bool(will_delegate), "is_clinical": is_clinical})

        mr_summary: Dict[str, Any] = {}
        admin_info: Dict[str, Any] = {}

        if will_delegate:
            await event_queue.put_status_update(TaskStatus.RUNNING, f"Delegating to MR Agent for {claim['mr_id']}")
            prompt = f"mr_id={claim['mr_id']}"
            mr_summary = await _call_mr_agent(MR_BASE_URL, prompt, event_queue, transcript)
            await event_queue.put_status_update(TaskStatus.RUNNING, "Adjudicating with LLM")
            transcript.add("claims_agent", "adjudication_start", {})
            decision = await evaluate_claim_with_llm(claim, mr_summary)
            transcript.add("claims_agent", "final_decision", {"decision": decision})
            result = {
                "mode": "delegated",
                "correlation_id": corr_id,
                "claim_id": claim_id,
                "decision": decision,
                "mr_summary": mr_summary,
                "transcript": transcript.events,
            }
        else:
            # Local/admin-only answer (no MR stream)
            admin_info = build_admin_info(claim)
            transcript.add("claims_agent", "local_only", {"reason": "admin_query_or_no_keywords"})
            result = {
                "mode": "local",
                "correlation_id": corr_id,
                "claim_id": claim_id,
                "admin_info": admin_info,
                "transcript": transcript.events,
            }

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
        description="Adjudicates claims; delegates to MR for clinical asks; returns a transcript.",
        tags=["claims","adjudication","medical","audit"],
        examples=["claim_id=CLM-1001 summarize MR", "claim_id=CLM-1002 show status and EOB notes"],
    )
    return AgentCard(
        name="Claims Agent",
        description="Claims adjudication with on-demand MR delegation and full transcript logging.",
        url=base_url,
        version="1.3.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill],
    )

# --------------------------------------------------------------------------------------
# A2A SERVER (Claims)
# --------------------------------------------------------------------------------------
def start_a2a_server_in_thread():
    base_url = f"http://localhost:{CLAIMS_PORT}"
    server = A2AServer(
        agent_card=build_agent_card(base_url),
        agent_executor=ClaimsAgentExecutor(),
        request_handler=DefaultA2ARequestHandler(),
    )
    app = server.app
    config = uvicorn.Config(app, host=HOST, port=CLAIMS_PORT, log_level="info")
    server_ = uvicorn.Server(config)
    t = threading.Thread(target=server_.run, daemon=True)
    t.start()
    return t

# --------------------------------------------------------------------------------------
# GRADIO VIEWER (left: chat, right: relay; default shows ONLY MR events)
# --------------------------------------------------------------------------------------
def css_theme() -> str:
    c = COLORS; f = FONTS
    return f"""
:root {{
  --bg: {c['bg']};
  --panel: {c['panel']};
  --text: {c['text']};
  --accent: {c['accent']};
  --claims: {c['claims']};
  --mr: {c['mr']};
  --user: {c['user']};
  --error: {c['error']};
  --status: {c['status']};
}}
.gradio-container {{ background: var(--bg); color: var(--text); }}
.section {{ background: var(--panel); border-radius: 14px; padding: 12px; }}
h3.title {{ margin: 0 0 8px 0; color: var(--accent); font-weight: 600; }}
.chat-wrap .message-user {{ color: var(--user); }}
.chat-wrap .message-assistant {{ color: var(--claims); }}
#transcript_box {{
  height: 560px; overflow: auto; background: #0e1320; border-radius: 10px; padding: 12px;
  font-family: {f['mono']}; font-size: 13px; line-height: 1.35;
  border: 1px solid #1e2433;
}}
.a2a-event {{ margin: 6px 0; padding: 6px 8px; border-radius: 8px; border: 1px solid #1e2433; }}
.a2a-event .ts {{ opacity: 0.65; font-size: 11px; margin-right: 8px; }}
.a2a-event.user  {{ background: rgba(154,160,166,.1); border-color: rgba(154,160,166,.25); color: var(--user);   }}
.a2a-event.claims{{ background: rgba(102,178,255,.08); border-color: rgba(102,178,255,.3); color: var(--claims); }}
.a2a-event.mr    {{ background: rgba(111,224,168,.08); border-color: rgba(111,224,168,.3); color: var(--mr);     }}
.a2a-event.error {{ background: rgba(255,107,107,.08); border-color: rgba(255,107,107,.3); color: var(--error);  }}
.a2a-event.status{{ background: rgba(189,189,189,.06); border-color: rgba(189,189,189,.25); color: var(--status);}}
.code {{ white-space: pre-wrap; word-wrap: break-word; }}
"""

def _origin_class(actor: str, kind: str) -> str:
    if "error" in kind: return "error"
    if actor == "mr_agent": return "mr"
    if actor == "claims_agent": return "claims"
    if actor == "user": return "user"
    return "status"

def humanize_events(events: List[dict], include: set[str], show_local: bool) -> str:
    """
    Filter + render transcript events into human friendly HTML.

    include: subset of {"Status", "Messages", "Artifacts", "Errors"}.
    show_local: False => only show MR-origin events (default for a truly empty pane in local flows).
    """
    html_lines: List[str] = []
    seen_status = set()

    for e in events:
        actor = e.get("actor", "system")
        kind = e.get("kind", "event")
        pay = e.get("payload", {})
        ts = e.get("ts", "")

        # Skip local events unless toggle is ON
        if not show_local and actor != "mr_agent":
            continue

        if kind.startswith("stream_event"):
            msg_text = (pay or {}).get("message_text")
            status = (pay or {}).get("status")
            if status and "Status" in include:
                key = f"{actor}:{status}"
                if key not in seen_status:
                    seen_status.add(key)
                    html_lines.append(
                        f'<div class="a2a-event status"><span class="ts">{ts}</span><b>Status</b> — <i>{actor}</i>: {status}</div>'
                    )
            if msg_text and "Messages" in include:
                clip = msg_text if len(msg_text) < 600 else (msg_text[:600] + "…")
                html_lines.append(
                    f'<div class="a2a-event {_origin_class(actor, kind)}"><span class="ts">{ts}</span><b>{actor}</b>: <span class="code">{gr.utils.markdown_to_html(clip)}</span></div>'
                )
            artifact = (pay or {}).get("artifact")
            if artifact and "Artifacts" in include:
                html_lines.append(
                    f'<div class="a2a-event {_origin_class(actor, kind)}"><span class="ts">{ts}</span><b>{actor} artifact</b>: {artifact}</div>'
                )
            continue

        # Other (we hide these by default when show_local=False)
        if "error" in kind and "Errors" in include:
            html_lines.append(
                f'<div class="a2a-event error"><span class="ts">{ts}</span><b>Error</b>: {pay}</div>'
            )

    return "\n".join(html_lines) or '<div class="a2a-event status">No events</div>'

async def send_to_claims_a2a(base_url: str, text: str) -> dict:
    client = await A2AClient.get_client_from_agent_card_url(base_url=base_url)
    try:
        req = SendStreamingMessageRequest(message=Message(role="user", parts=[TextPart(text=text)]))
        final_json = {}
        async for evt in client.send_message_streaming(req):
            msg = getattr(evt, "message", None)
            if isinstance(msg, Message):
                body = ""
                for part in (msg.parts or []):
                    if isinstance(part, TextPart):
                        body += part.text
                if body:
                    try:
                        final_json = json.loads(body)
                    except Exception:
                        pass
        return final_json
    finally:
        try:
            await client.aclose()
        except Exception:
            pass

def make_ui():
    with gr.Blocks(title="Claims Agent | A2A Viewer", css=css_theme()) as demo:
        gr.Markdown(f"### <span style='color:{COLORS['accent']}'>Claims Agent (A2A) — Demo Viewer</span>")
        with gr.Row():
            with gr.Column(scale=5, elem_classes=["section", "chat-wrap"]):
                gr.Markdown("#### Chat")
                chat = gr.Chatbot(height=560, show_copy_button=True, avatar_images=(None, None))
                inp = gr.Textbox(placeholder="Type a prompt, e.g. claim_id=CLM-1001 summarize MR", autofocus=True)
                send = gr.Button("Send", variant="primary")
            with gr.Column(scale=5, elem_classes=["section"]):
                gr.Markdown("#### A2A Relay (MR events)")
                filters = gr.CheckboxGroup(
                    choices=["Status", "Messages", "Artifacts", "Errors"],
                    value=["Status", "Messages"],
                    label="Show",
                )
                show_local = gr.Checkbox(value=False, label="Show local events (Claims/System)")
                transcript_box = gr.HTML(elem_id="transcript_box")

        # App state
        history_meta = gr.State([])  # one dict per turn: {"correlation_id":..., "transcript":[...]}
        base_url_state = gr.State(f"http://localhost:{CLAIMS_PORT}")

        async def on_send(user_msg, chat_log: List[Tuple[str, str]], meta: List[dict], base_url, shown, include_local):
            if not user_msg:
                return gr.update(), chat_log, meta

            chat_log = chat_log + [(user_msg, "")]
            result = await send_to_claims_a2a(base_url, user_msg)

            # Render assistant bubble:
            if result.get("mode") == "local":
                info = result.get("admin_info", {})
                assistant_text = (
                    f"**Status:** {info.get('claim_status','?')}\n\n"
                    f"**Amount:** ${info.get('claim_amount','?')}\n\n"
                    f"**EOB notes:** {info.get('eob_notes','?')}\n\n"
                    f"**Member:** {info.get('member_id','?')}  •  **MR linked:** {info.get('mr_linked')}"
                )
            else:
                decision = result.get("decision", {})
                assistant_text = (
                    f"**Decision:** {decision.get('disposition','?')}\n\n"
                    f"**Allowed (est):** ${decision.get('allowed_amount_estimate','?')}\n\n"
                    f"**Reasons:** " + "; ".join(decision.get('reasons', []))
                )
            chat_log[-1] = (user_msg, assistant_text)

            meta = meta + [{
                "correlation_id": result.get("correlation_id"),
                "transcript": result.get("transcript", []),
            }]

            html = humanize_events(meta[-1]["transcript"], include=set(shown or []), show_local=bool(include_local))
            return "", chat_log, meta, html

        async def on_select(evt: gr.SelectData, chat_log: List[Tuple[str, str]], meta: List[dict], shown, include_local):
            idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
            if idx is None or idx >= len(meta) or idx < 0:
                return gr.update()
            html = humanize_events(meta[idx]["transcript"], include=set(shown or []), show_local=bool(include_local))
            return html

        def on_filter_change(shown, include_local, meta: List[dict]):
            if not meta:
                return gr.update()
            return humanize_events(meta[-1]["transcript"], include=set(shown or []), show_local=bool(include_local))

        send.click(
            on_send,
            inputs=[inp, chat, history_meta, base_url_state, filters, show_local],
            outputs=[inp, chat, history_meta, transcript_box],
            queue=True,
        )
        chat.select(
            on_select,
            inputs=[chat, history_meta, filters, show_local],
            outputs=[transcript_box],
        )
        filters.change(
            on_filter_change,
            inputs=[filters, show_local, history_meta],
            outputs=[transcript_box],
        )
        show_local.change(
            on_filter_change,
            inputs=[filters, show_local, history_meta],
            outputs=[transcript_box],
        )

    return demo

# --------------------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    # 1) Start A2A server (Claims) in a background thread
    start_a2a_server_in_thread()

    # 2) Launch Gradio viewer
    ui = make_ui()
    ui.launch(server_name="0.0.0.0", server_port=GRADIO_PORT)