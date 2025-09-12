# claims_agent_app.py
# A2A 0.3.x-compliant Claims Agent + Gradio viewer (single process)
# - Left: chat with Claims
# - Right: A2A relay log (color-coded, per-turn, human-readable)
#
# Requires: a2a-sdk>=0.3,<0.4, gradio, uvicorn, httpx

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from typing import Any, Dict, List, Tuple, Union, Optional

import httpx
import gradio as gr
import uvicorn

# --- A2A SDK (0.3.x) imports ---
from a2a.types import (  # core schema types
    AgentCard,
    AgentCapabilities,
    AgentSkill,
    AgentProvider,
    Message,
    Artifact,
    DataPart,
)

from a2a.server.agent_execution import AgentExecutor, RequestContext  # executor interface
from a2a.server.request_handlers import DefaultRequestHandler          # JSON-RPC handler
from a2a.server.events import EventQueue, InMemoryQueueManager         # streaming queue
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater            # task store/updater
from a2a.server.apps.jsonrpc import A2AStarletteApplication            # Starlette app
from a2a.utils.message import (                                        # helper constructors
    new_agent_text_message,
    new_user_text_message,
)

from a2a.client.client import ClientConfig                             # client config
from a2a.client.client_factory import ClientFactory                    # client factory
from a2a.client.card_resolver import A2ACardResolver                   # resolves agent card

# -----------------------------
# Config
# -----------------------------
CLAIMS_HOST = os.environ.get("CLAIMS_AGENT_HOST", "127.0.0.1")
CLAIMS_PORT = int(os.environ.get("CLAIMS_AGENT_PORT", "8000"))
CLAIMS_BASE_URL = f"http://{CLAIMS_HOST}:{CLAIMS_PORT}"

# Where the MR agent is running
MR_BASE_URL = os.environ.get("MR_AGENT_BASE_URL", "http://127.0.0.1:9001")

# Gradio port
GRADIO_PORT = int(os.environ.get("GRADIO_PORT", "7860"))

# -----------------------------
# Utility: clinical intent router
# -----------------------------
_CLINICAL_KEYWORDS = re.compile(
    r"\b(med(ical|s)?\b|medication(s)?\b|drug(s)?\b|rx\b|diagnos(es|is)\b|diagnostic\b|"
    r"icd\b|cpt\b|procedure(s)?\b|chart(s)?\b|note(s)?\b|mr\b|record(s)?\b|hist(ory)?\b)\b",
    re.I,
)

def is_clinical_query(text: str) -> bool:
    return bool(_CLINICAL_KEYWORDS.search(text or ""))

# -----------------------------
# Relay log shaping (human readable)
# -----------------------------
def _compact_text(t: str, max_len: int = 320) -> str:
    t = (t or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "…"

def _event_to_log_entry(source: str, event: Any) -> Dict[str, Any]:
    """
    Normalize client-streamed events into a small dict for human-friendly display.
    `source` is 'claims' or 'mr'.
    """
    entry: Dict[str, Any] = {"who": source, "kind": None}

    # Events seen by the client: either a Message, or a (Task, UpdateEvent) tuple
    # BaseClient.send_message yields:
    #   - (Task, TaskStatusUpdateEvent|TaskArtifactUpdateEvent|None) OR
    #   - Message (final)
    # We won't rely on full schema—just capture a few user-facing fields.

    # Message (final response case)
    if isinstance(event, Message):
        entry["kind"] = "message"
        # Collect all text parts (if any)
        texts = []
        for p in getattr(event, "parts", []) or []:
            if getattr(p, "kind", None) == "text":
                texts.append(getattr(p, "text", "") or "")
        entry["text"] = _compact_text("\n".join(texts))
        entry["metadata"] = getattr(event, "metadata", {}) or {}
        return entry

    # Tuple (Task, UpdateEvent or None)
    if isinstance(event, tuple) and len(event) == 2:
        task, upd = event
        entry["task_id"] = getattr(task, "id", None)

        if upd is None:
            entry["kind"] = "task-update"
            entry["state"] = getattr(getattr(task, "status", None), "state", None)
            return entry

        upd_type = type(upd).__name__
        entry["kind"] = upd_type

        # TaskStatusUpdateEvent fields we care about
        if "TaskStatusUpdateEvent" in upd_type:
            entry["state"] = getattr(upd, "state", None)
            # some servers carry a message on status events
            msg = getattr(upd, "message", None)
            if isinstance(msg, Message):
                texts = []
                for p in getattr(msg, "parts", []) or []:
                    if getattr(p, "kind", None) == "text":
                        texts.append(getattr(p, "text", "") or "")
                if texts:
                    entry["text"] = _compact_text("\n".join(texts))
            return entry

        # TaskArtifactUpdateEvent
        if "TaskArtifactUpdateEvent" in upd_type:
            art = getattr(upd, "artifact", None)
            if isinstance(art, Artifact):
                entry["artifact_name"] = getattr(art, "name", None)
                entry["artifact_desc"] = getattr(art, "description", None)
            entry["state"] = getattr(upd, "state", None)
            return entry

    # Fallback (unknown shape)
    entry["kind"] = "event"
    entry["raw"] = str(event)
    return entry

def _render_log_html(log: List[Dict[str, Any]]) -> str:
    """
    Return colored HTML for the relay log.
    Colors are controlled by CSS variables for easy theming.
    """
    if not log:
        return "<div class='relay-empty'>No A2A relay events.</div>"

    def badge(who: str) -> str:
        if who == "claims":
            return "<span class='tag tag-claims'>CLAIMS</span>"
        if who == "mr":
            return "<span class='tag tag-mr'>MR</span>"
        return f"<span class='tag'>{who.upper()}</span>"

    rows = []
    for e in log:
        who = e.get("who", "?")
        kind = e.get("kind", "event")
        state = e.get("state", "")
        text = e.get("text", "")
        art_name = e.get("artifact_name")
        art_desc = e.get("artifact_desc")

        meta_line = " • ".join([k for k in [kind, state] if k])
        body = ""
        if text:
            body += f"<div class='msg'>{gr.utils.sanitize_html(text)}</div>"
        if art_name or art_desc:
            body += "<div class='artifact'>"
            if art_name:
                body += f"<div><b>artifact:</b> {gr.utils.sanitize_html(art_name)}</div>"
            if art_desc:
                body += f"<div><b>desc:</b> {gr.utils.sanitize_html(art_desc)}</div>"
            body += "</div>"

        rows.append(
            f"<div class='relay-row relay-{who}'>"
            f"  <div class='relay-head'>{badge(who)}<span class='meta'>{gr.utils.sanitize_html(meta_line)}</span></div>"
            f"  <div class='relay-body'>{body}</div>"
            f"</div>"
        )
    return "<div class='relay'>" + "\n".join(rows) + "</div>"

# -----------------------------
# Claims Agent Executor
# -----------------------------
class ClaimsAgentExecutor(AgentExecutor):
    """
    Demo Claims agent:
      - Answers admin/claim questions locally (single-agent).
      - Delegates clinical questions to MR agent via A2A and returns final answer.
      - Captures MR streaming events into a slim 'relay_log' attached to the final Message.metadata.
    """

    def __init__(self, mr_base_url: str):
        self.mr_base_url = mr_base_url

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Minimal demo doesn't maintain long-lived tasks; nothing to cancel.
        pass

    async def _answer_admin_locally(self, user_text: str) -> str:
        # Stub some admin details; in a real system you'd hit your claims DB.
        if "status" in user_text.lower():
            return "Claim 88421 is APPROVED. Allowed amount: $326.47. EOB available."
        if "amount" in user_text.lower():
            return "Paid amount: $291.82. Member responsibility: $34.65."
        if "eob" in user_text.lower() or "notes" in user_text.lower():
            return "EOB notes: claim bundled per plan rules; no member appeal on file."
        return "I can help with status, amounts, and EOB notes. What would you like to check?"

    async def _ask_mr_agent(self, prompt: str) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Send a clinical prompt to MR agent, stream and collect events,
        and return (final_text, relay_log).
        """
        relay_log: List[Dict[str, Any]] = []
        final_text_chunks: List[str] = []

        # Build MR client
        async with httpx.AsyncClient(timeout=30.0) as httpx_client:
            resolver = A2ACardResolver(httpx_client=httpx_client, base_url=self.mr_base_url)
            card = await resolver.get_agent_card()  # public card
            factory = ClientFactory(ClientConfig(streaming=True, httpx_client=httpx_client))
            async with factory.get(card) as client:
                user_msg = new_user_text_message(prompt)
                async for ev in client.send_message(user_msg):
                    # Mirror MR events into the relay log (human-readable)
                    relay_log.append(_event_to_log_entry("mr", ev))

                    # Try to accumulate text if a Message appears
                    if isinstance(ev, Message):
                        # collect message text parts
                        for p in getattr(ev, "parts", []) or []:
                            if getattr(p, "kind", None) == "text":
                                final_text_chunks.append(getattr(p, "text", "") or "")
                    elif isinstance(ev, tuple):
                        task, upd = ev
                        # Some servers put text on status updates too
                        if upd and getattr(upd, "message", None) and isinstance(upd.message, Message):
                            for p in getattr(upd.message, "parts", []) or []:
                                if getattr(p, "kind", None) == "text":
                                    final_text_chunks.append(getattr(p, "text", "") or "")

        final_text = "\n".join([c for c in final_text_chunks if c]).strip() or \
                     "MR returned no textual content."
        return final_text, relay_log

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """
        Core execution: decide local vs delegation; publish streaming updates and final message.
        """
        updater = TaskUpdater(event_queue=event_queue, task_store=None)  # task_store only needed if persisting
        user_text = context.get_user_input()

        # 'submitted'
        await updater.submit(new_agent_text_message("Received your request."))
        # 'working'
        await updater.start_work(new_agent_text_message("Analyzing your request…"))

        try:
            if not is_clinical_query(user_text):
                # Single-agent handling
                await updater.add_artifact(Artifact(
                    name="route",
                    description="Decision: local",
                    parts=[DataPart(kind="text", text="handled_by=claims")],
                ))
                answer = await self._answer_admin_locally(user_text)
                # Final: attach empty relay log
                final = new_agent_text_message(
                    answer,
                    metadata={"relay_log": [], "relay_summary": "Local-only response (no delegation)."}
                )
                await updater.complete(final)
                return

            # Delegation to MR
            await updater.add_artifact(Artifact(
                name="route",
                description="Decision: delegated to MR",
                parts=[DataPart(kind="text", text="handled_by=mr;reason=clinical-query")],
            ))

            # Let the client know we are delegating
            await updater.add_status_update(new_agent_text_message("Delegating clinical portion to MR…"))

            mr_answer, mr_log = await self._ask_mr_agent(user_text)

            # Publish an artifact snapshot of the relay for audit (compact)
            await updater.add_artifact(Artifact(
                name="relay-log",
                description="Captured MR relay (compact)",
                parts=[DataPart(kind="text", text=json.dumps(mr_log)[:4000])],  # safe bound
            ))

            final = new_agent_text_message(
                f"Clinical summary from MR:\n{mr_answer}",
                metadata={
                    "relay_log": mr_log,
                    "relay_summary": "Claims → MR delegated; MR supplied clinical content."
                }
            )
            await updater.complete(final)

        except Exception as e:
            await updater.fail(new_agent_text_message(f"Failed: {e}"))

# -----------------------------
# Build the A2A Starlette app
# -----------------------------
def build_agent_card() -> AgentCard:
    return AgentCard(
        name="Claims Agent",
        version="1.0.0",
        description="Handles claim status/amounts and delegates clinical questions to MR.",
        url=CLAIMS_BASE_URL,
        provider=AgentProvider(organization="ClaimsCo", url="https://example.org/claims"),
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[
            AgentSkill(
                id="respond",
                name="Respond",
                description="General Q&A for claims; clinical items are delegated."
            )
        ],
    )

def build_starlette_app() -> Any:
    executor = ClaimsAgentExecutor(mr_base_url=MR_BASE_URL)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        queue_manager=InMemoryQueueManager(),
    )
    app = A2AStarletteApplication(
        agent_card=build_agent_card(),
        http_handler=request_handler,
    ).build()
    return app

# -----------------------------
# Run A2A server in a background thread
# -----------------------------
def start_a2a_server_in_thread():
    def _run():
        uvicorn.run(
            build_starlette_app(),
            host=CLAIMS_HOST,
            port=CLAIMS_PORT,
            log_level="info",
        )
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t

# -----------------------------
# Gradio UI
# -----------------------------
CSS = """
:root {
  --bg: #0f1216; --fg:#eaeef5; --muted:#a4adbd;
  --box:#151a21; --border:#232a34;
  --claims:#3fa6ff; --mr:#5bd69f;        /* change these for color-coding */
}
.gradio-container { font-family: Inter, system-ui, sans-serif; }
#layout { gap: 10px; }
.panel { background: var(--box); border: 1px solid var(--border); border-radius: 12px; padding: 12px; }
.panel h3 { margin: 4px 0 10px; color: var(--fg); font-size: 14px; font-weight: 600; }
.relay-empty { color: var(--muted); padding: 16px; }
.relay { display: flex; flex-direction: column; gap: 10px; }
.relay-row { border: 1px solid var(--border); border-radius: 10px; padding: 10px; background: #0d1117; }
.relay-row.relay-claims { border-left: 3px solid var(--claims); }
.relay-row.relay-mr { border-left: 3px solid var(--mr); }
.relay-head { display:flex; align-items:center; gap:8px; color: var(--muted); font-size: 12px; }
.tag { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:600; color:#0b0e11; background:#7b8a9b; }
.tag-claims { background: var(--claims); }
.tag-mr { background: var(--mr); }
.meta { margin-left:auto; font-size:11px; color: var(--muted); }
.relay-body .msg { margin-top:6px; white-space: pre-wrap; color: var(--fg); }
.artifact { margin-top:6px; color: var(--muted); font-size:12px; }
"""

# We keep each chat turn's relay log in a parallel list, same index as chatbot turns.
# State shapes:
# - chat_history: list[tuple[str, str]]
# - relay_logs: list[list[dict]]  (per turn)
async def _send_to_claims_and_capture(prompt: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Use A2A client to talk to this same Claims server and capture its embedded relay_log."""
    async with httpx.AsyncClient(timeout=30.0) as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=CLAIMS_BASE_URL)
        card = await resolver.get_agent_card()
        factory = ClientFactory(ClientConfig(streaming=True, httpx_client=httpx_client))
        async with factory.get(card) as client:
            user_msg = new_user_text_message(prompt)
            final_text_chunks: List[str] = []
            relay_from_claims: List[Dict[str, Any]] = []
            final_message_metadata: Dict[str, Any] = {}

            async for ev in client.send_message(user_msg):
                # For UX, log the CLAIMS-side stream too (useful for single-agent visibility)
                relay_from_claims.append(_event_to_log_entry("claims", ev))

                # Aggregate any text delivered as Message or on status message
                if isinstance(ev, Message):
                    final_message_metadata = getattr(ev, "metadata", {}) or {}
                    for p in getattr(ev, "parts", []) or []:
                        if getattr(p, "kind", None) == "text":
                            final_text_chunks.append(getattr(p, "text", "") or "")
                elif isinstance(ev, tuple):
                    task, upd = ev
                    if upd and getattr(upd, "message", None) and isinstance(upd.message, Message):
                        for p in getattr(upd.message, "parts", []) or []:
                            if getattr(p, "kind", None) == "text":
                                final_text_chunks.append(getattr(p, "text", "") or "")

            # Prefer final message text; fall back if empty
            answer_text = "\n".join([c for c in final_text_chunks if c]).strip() or "No text returned."

            # If the server attached MR relay in final message metadata, merge it (right pane should show only MR by default)
            mr_log = []
            if isinstance(final_message_metadata, dict):
                mr_log = final_message_metadata.get("relay_log", []) or []

            # Our right pane wants to visualize the delegated hops; if none, we keep it empty.
            return answer_text, (mr_log or [])

def build_gradio_app():
    with gr.Blocks(css=CSS, theme=gr.themes.Soft()) as demo:
        gr.Markdown("## Claims Agent (A2A 0.3.x) — Chat + Relay Viewer")

        with gr.Row(elem_id="layout"):
            # Left: basic chat
            with gr.Column(scale=6):
                left_panel = gr.Group(elem_classes=["panel"])
                with left_panel:
                    gr.Markdown("### Chat")
                    chatbot = gr.Chatbot(height=540, type="messages", show_copy_button=True)
                    with gr.Row():
                        prompt = gr.Textbox(
                            label="Message",
                            placeholder="Ask for claim status/amounts (local) or clinical info (delegates to MR)...",
                            scale=7
                        )
                        send_btn = gr.Button("Send", variant="primary", scale=1)

            # Right: relay viewer
            with gr.Column(scale=6):
                right_panel = gr.Group(elem_classes=["panel"])
                with right_panel:
                    gr.Markdown("### A2A Relay")
                    relay_html = gr.HTML("<div class='relay-empty'>Click a turn in the chat to view its A2A relay.</div>")

        # hidden state
        relay_logs_state = gr.State([])  # list of per-turn logs

        async def on_send(user_text, history: List[Tuple[str, str]], logs: List[List[Dict[str, Any]]]):
            user_text = (user_text or "").strip()
            if not user_text:
                return gr.update(), history, logs

            # Call Claims (which might delegate to MR)
            answer, mr_log = await _send_to_claims_and_capture(user_text)

            # Update chat
            new_history = (history or []) + [(user_text, answer)]
            new_logs = (logs or []) + [mr_log]  # store only MR relay for the turn
            return gr.update(value=""), new_history, new_logs

        async def on_select(evt: gr.SelectData, history: List[Tuple[str, str]], logs: List[List[Dict[str, Any]]]):
            # evt.index is the (user, assistant) message pair index in Chatbot
            idx = evt.index if isinstance(evt.index, int) else None
            if idx is None or not logs or idx >= len(logs):
                return gr.update(value="<div class='relay-empty'>No relay captured for this turn.</div>")
            log = logs[idx] or []
            return gr.update(value=_render_log_html(log))

        send_btn.click(
            fn=on_send,
            inputs=[prompt, chatbot, relay_logs_state],
            outputs=[prompt, chatbot, relay_logs_state],
        )
        chatbot.select(
            fn=on_select,
            inputs=[chatbot, relay_logs_state],
            outputs=[relay_html],
        )
    return demo

# -----------------------------
# Entrypoint
# -----------------------------
if __name__ == "__main__":
    # Start A2A JSON-RPC server in the background
    start_a2a_server_in_thread()
    # Launch Gradio UI
    app = build_gradio_app()
    app.queue().launch(server_name="0.0.0.0", server_port=GRADIO_PORT, show_api=False)