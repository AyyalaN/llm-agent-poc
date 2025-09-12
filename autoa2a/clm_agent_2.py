# claims_agent_app.py
# A2A 0.3.x-compliant Claims Agent with AutoGen AgentChat + Gradio viewer
# - Left pane: chat with Claims
# - Right pane: A2A relay (MR events), clickable per turn
# Deps: a2a-sdk>=0.3,<0.4, autogen-agentchat, autogen-ext[openai], gradio, httpx, uvicorn

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from typing import Any, Dict, List, Tuple

import httpx
import gradio as gr
import uvicorn

# ---------- AutoGen (AgentChat) ----------
from autogen_agentchat.agents import AssistantAgent
from autogen_ext.models.openai import OpenAIChatCompletionClient  # OpenAI / OpenAI-compatible client
# Docs for these APIs: AssistantAgent, OpenAIChatCompletionClient, run() returns TaskResult.messages
# https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/quickstart.html
# https://microsoft.github.io/autogen/stable/reference/python/autogen_ext.models.openai.html

# ---------- A2A SDK 0.3.x ----------
from a2a.types import (
    AgentCard, AgentCapabilities, AgentSkill, AgentProvider,
    Message, Artifact, DataPart
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.events import EventQueue, InMemoryQueueManager
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.server.apps.jsonrpc import A2AStarletteApplication

from a2a.client.client_factory import ClientFactory, ClientConfig
from a2a.client.card_resolver import A2ACardResolver

# -----------------------------
# Config / ENV
# -----------------------------
CLAIMS_HOST = os.getenv("CLAIMS_AGENT_HOST", "127.0.0.1")
CLAIMS_PORT = int(os.getenv("CLAIMS_AGENT_PORT", "8000"))
CLAIMS_BASE_URL = f"http://{CLAIMS_HOST}:{CLAIMS_PORT}"

MR_BASE_URL = os.getenv("MR_AGENT_BASE_URL", "http://127.0.0.1:9001")
GRADIO_PORT = int(os.getenv("GRADIO_PORT", "7860"))

# AutoGen model config (OpenAI or compatible)
OPENAI_MODEL = os.getenv("MODEL_ID", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # optional if your endpoint doesn’t need it
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")  # e.g., http://localhost:8000/v1 for vLLM

# Create one shared AutoGen model client (reused across requests)
MODEL_CLIENT = OpenAIChatCompletionClient(
    model=OPENAI_MODEL,
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_BASE_URL,  # omit if using OpenAI hosted
    # for OpenAI-compatible endpoints, you can also pass model_info={"supports_function_calling": False}
)

# -----------------------------
# Intent routing (local vs clinical)
# -----------------------------
_CLINICAL_RE = re.compile(
    r"\b(med(ical|s)?|medication(s)?|drug(s)?|rx|diagnos(es|is)|icd|labs?|clinical|summary|mr|record|chart|history)\b",
    re.I,
)
def is_clinical_query(text: str) -> bool:
    return bool(_CLINICAL_RE.search(text or ""))

# -----------------------------
# Relay shaping (human-readable)
# -----------------------------
def _compact(t: str, n: int = 320) -> str:
    t = (t or "").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"

def _event_to_log_entry(who: str, ev: Any) -> Dict[str, Any]:
    # Client stream yields either a Message, or a (Task, UpdateEvent) tuple
    entry: Dict[str, Any] = {"who": who, "kind": "event"}

    # Final message
    if isinstance(ev, Message):
        entry["kind"] = "message"
        texts = []
        for p in getattr(ev, "parts", []) or []:
            if getattr(p, "kind", None) == "text":
                texts.append(getattr(p, "text", "") or "")
        entry["text"] = _compact("\n".join(texts))
        entry["metadata"] = getattr(ev, "metadata", {}) or {}
        return entry

    # (Task, Update)
    if isinstance(ev, tuple) and len(ev) == 2:
        task, upd = ev
        entry["task_id"] = getattr(task, "id", None)
        if upd is None:
            entry["kind"] = "task-update"
            entry["state"] = getattr(getattr(task, "status", None), "state", None)
            return entry
        tname = type(upd).__name__
        entry["kind"] = tname
        entry["state"] = getattr(upd, "state", None)
        # some servers attach a Message on status updates
        msg = getattr(upd, "message", None)
        if isinstance(msg, Message):
            texts = []
            for p in getattr(msg, "parts", []) or []:
                if getattr(p, "kind", None) == "text":
                    texts.append(getattr(p, "text", "") or "")
            if texts:
                entry["text"] = _compact("\n".join(texts))
        # artifact updates
        art = getattr(upd, "artifact", None)
        if art is not None:
            entry["artifact_name"] = getattr(art, "name", None)
            entry["artifact_desc"] = getattr(art, "description", None)
        return entry

    entry["raw"] = str(ev)
    return entry

def _render_log_html(log: List[Dict[str, Any]]) -> str:
    if not log:
        return "<div class='relay-empty'>No A2A relay events.</div>"
    def badge(who: str) -> str:
        return f"<span class='tag tag-{who}'>" + (who.upper()) + "</span>"
    rows = []
    for e in log:
        who = e.get("who", "?")
        kind = e.get("kind", "")
        state = e.get("state", "")
        text = e.get("text", "")
        meta = " • ".join(x for x in [kind, state] if x)
        body = ""
        if text:
            body += f"<div class='msg'>{gr.utils.sanitize_html(text)}</div>"
        if e.get("artifact_name") or e.get("artifact_desc"):
            body += "<div class='artifact'>"
            if e.get("artifact_name"): body += f"<div><b>artifact:</b> {gr.utils.sanitize_html(e['artifact_name'])}</div>"
            if e.get("artifact_desc"): body += f"<div><b>desc:</b> {gr.utils.sanitize_html(e['artifact_desc'])}</div>"
            body += "</div>"
        rows.append(
            f"<div class='relay-row relay-{who}'>"
            f"  <div class='relay-head'>{badge(who)}<span class='meta'>{gr.utils.sanitize_html(meta)}</span></div>"
            f"  <div class='relay-body'>{body}</div>"
            f"</div>"
        )
    return "<div class='relay'>" + "\n".join(rows) + "</div>"

# -----------------------------
# AutoGen composition (one agent per call, shared model client)
# -----------------------------
async def compose_with_autogen(prompt: str) -> str:
    """
    Use AutoGen AssistantAgent to produce a well-phrased answer from our structured data.
    """
    agent = AssistantAgent(
        name="claims_assistant",
        model_client=MODEL_CLIENT,
        system_message=(
            "You are a claims desk agent. Be concise and accurate. "
            "If CLAIM JSON is present, summarize status/amounts/EOB clearly. "
            "If MR_TEXT is present, summarize clinical info succinctly. "
            "Never invent data."
        ),
    )
    result = await agent.run(task=prompt)  # returns TaskResult; take last message’s content
    msgs = getattr(result, "messages", []) or []
    if not msgs:
        return "No response."
    last = msgs[-1]
    content = getattr(last, "content", None) or getattr(last, "text", None)
    return content or "No content."

# -----------------------------
# Claims Agent (A2A executor)
# -----------------------------
class ClaimsAgentExecutor(AgentExecutor):
    def __init__(self, mr_base_url: str):
        self.mr_base_url = mr_base_url

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # no-op for demo
        pass

    async def _delegate_to_mr(self, user_text: str) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Stream from MR agent and capture a compact relay log.
        """
        relay: List[Dict[str, Any]] = []
        final_chunks: List[str] = []

        async with httpx.AsyncClient(timeout=30.0) as httpx_client:
            resolver = A2ACardResolver(httpx_client=httpx_client, base_url=self.mr_base_url)
            mr_card = await resolver.get_agent_card()  # /.well-known/agent-card.json
            factory = ClientFactory(ClientConfig(streaming=True, httpx_client=httpx_client))
            async with factory.get(mr_card) as mr_client:
                # send the user's text as-is; attach simple metadata if desired
                async for ev in mr_client.send_message(Message(role="user", parts=[{"kind":"text","text":user_text}])):
                    relay.append(_event_to_log_entry("mr", ev))
                    if isinstance(ev, Message):
                        for p in getattr(ev, "parts", []) or []:
                            if getattr(p, "kind", None) == "text":
                                final_chunks.append(getattr(p, "text", "") or "")
                    elif isinstance(ev, tuple):
                        task, upd = ev
                        if upd and getattr(upd, "message", None) and isinstance(upd.message, Message):
                            for p in getattr(upd.message, "parts", []) or []:
                                if getattr(p, "kind", None) == "text":
                                    final_chunks.append(getattr(p, "text", "") or "")
        mr_text = "\n".join([c for c in final_chunks if c]).strip() or "MR returned no text."
        return mr_text, relay

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue=event_queue, task_store=None)

        user_text = context.get_user_input()
        await updater.submit(Message(role="assistant", parts=[{"kind":"text","text":"Received your request."}]))
        await updater.start_work(Message(role="assistant", parts=[{"kind":"text","text":"Analyzing …"}]))

        try:
            # Decide route
            if not is_clinical_query(user_text):
                # Local (single-agent) — use AutoGen to compose admin answer
                await updater.add_artifact(Artifact(
                    name="route", description="local", parts=[DataPart(kind="text", text="handled_by=claims")]
                ))
                claim_stub = {
                    "status": "Approved",
                    "allowed_amount": 326.47,
                    "member_responsibility": 34.65,
                    "eob_notes": "Bundled per plan rules; no appeal on file.",
                }
                prompt = f"CLAIM={json.dumps(claim_stub)}\nTASK=Write a 3-5 sentence answer covering status, allowed amount, member responsibility, and any EOB notes."
                final_text = await compose_with_autogen(prompt)
                await updater.complete(Message(role="assistant", parts=[{"kind":"text","text":final_text}], metadata={"relay_log": []}))
                return

            # Delegated (Claims → MR)
            await updater.add_artifact(Artifact(
                name="route", description="delegated", parts=[DataPart(kind="text", text="handled_by=mr")]
            ))
            await updater.add_status_update(Message(role="assistant", parts=[{"kind":"text","text":"Delegating clinical portion to MR …"}]))

            mr_text, mr_relay = await self._delegate_to_mr(user_text)

            # Ask AutoGen to compose a clean final answer that blends MR content
            prompt = (
                "MR_TEXT=\n" + mr_text + "\n\n"
                "TASK=Summarize the clinical info (diagnoses, meds, notable risks) in 3-6 bullets for a claims reviewer. "
                "Avoid duplicate lines and hedging."
            )
            final_text = await compose_with_autogen(prompt)

            await updater.add_artifact(Artifact(
                name="relay-log", description="MR relay (compact)",
                parts=[DataPart(kind="text", text=json.dumps(mr_relay)[:4000])]
            ))
            await updater.complete(Message(
                role="assistant",
                parts=[{"kind":"text","text":final_text}],
                metadata={"relay_log": mr_relay, "relay_summary": "Claims → MR delegated"}
            ))

        except Exception as e:
            await updater.fail(Message(role="assistant", parts=[{"kind":"text","text":f"Failed: {e}"}]))

# -----------------------------
# A2A Starlette app
# -----------------------------
def build_agent_card() -> AgentCard:
    return AgentCard(
        name="Claims Agent",
        version="1.0.0",
        description="Handles claim status/amounts locally; delegates clinical questions to MR.",
        url=CLAIMS_BASE_URL,
        provider=AgentProvider(organization="ClaimsCo", url="https://example.org/claims"),
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[AgentSkill(id="respond", name="Respond", description="General Q&A for claims; clinical items are delegated.")]
    )

def build_starlette_app():
    handler = DefaultRequestHandler(
        agent_executor=ClaimsAgentExecutor(mr_base_url=MR_BASE_URL),
        task_store=InMemoryTaskStore(),
        queue_manager=InMemoryQueueManager(),
    )
    return A2AStarletteApplication(agent_card=build_agent_card(), http_handler=handler).build()

def start_a2a_server_thread():
    def _run():
        uvicorn.run(build_starlette_app(), host=CLAIMS_HOST, port=CLAIMS_PORT, log_level="info")
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t

# -----------------------------
# Gradio UI (same UX: left chat, right relay)
# -----------------------------
CSS = """
:root {
  --bg: #0f1216; --fg:#eaeef5; --muted:#a4adbd;
  --box:#151a21; --border:#232a34;
  --claims:#3fa6ff; --mr:#5bd69f;
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

async def _send_to_claims(prompt: str) -> Tuple[str, List[Dict[str, Any]]]:
    async with httpx.AsyncClient(timeout=30.0) as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=CLAIMS_BASE_URL)
        card = await resolver.get_agent_card()
        factory = ClientFactory(ClientConfig(streaming=True, httpx_client=httpx_client))
        async with factory.get(card) as client:
            final_text_parts: List[str] = []
            final_meta: Dict[str, Any] = {}
            async for ev in client.send_message(Message(role="user", parts=[{"kind":"text","text":prompt}])):
                # (Optional) include CLAIMS-side stream in a separate log if you want
                if isinstance(ev, Message):
                    final_meta = getattr(ev, "metadata", {}) or {}
                    for p in getattr(ev, "parts", []) or []:
                        if getattr(p, "kind", None) == "text":
                            final_text_parts.append(getattr(p, "text", "") or "")
                elif isinstance(ev, tuple):
                    task, upd = ev
                    if upd and getattr(upd, "message", None) and isinstance(upd.message, Message):
                        for p in getattr(upd.message, "parts", []) or []:
                            if getattr(p, "kind", None) == "text":
                                final_text_parts.append(getattr(p, "text", "") or "")
            text = "\n".join([t for t in final_text_parts if t]).strip() or "No text."
            relay = (final_meta.get("relay_log") or []) if isinstance(final_meta, dict) else []
            return text, relay

def build_gradio():
    with gr.Blocks(css=CSS, title="Claims (A2A 0.3.x + AutoGen) — Chat + Relay") as demo:
        gr.Markdown("## Claims Agent — Chat (left) + A2A Relay (right)")

        with gr.Row(elem_id="layout"):
            with gr.Column(scale=6):
                with gr.Group(elem_classes=["panel"]):
                    gr.Markdown("### Chat")
                    chat = gr.Chatbot(height=540, type="messages", show_copy_button=True)
                    with gr.Row():
                        msg = gr.Textbox(placeholder="Ask status/amounts/EOB (local) or clinical info (delegates to MR)…", scale=7)
                        send = gr.Button("Send", variant="primary", scale=1)
            with gr.Column(scale=6):
                with gr.Group(elem_classes=["panel"]):
                    gr.Markdown("### A2A Relay (MR events)")
                    relay_html = gr.HTML("<div class='relay-empty'>Click a chat turn to view its A2A relay.</div>")

        relay_state = gr.State([])  # per-turn MR logs

        async def on_send(user_text, history: List[Tuple[str, str]], logs: List[List[Dict[str, Any]]]):
            user_text = (user_text or "").strip()
            if not user_text:
                return gr.update(), history, logs
            answer, mr_log = await _send_to_claims(user_text)
            history = (history or []) + [(user_text, answer)]
            logs = (logs or []) + [mr_log]
            return gr.update(value=""), history, logs

        def on_select(evt: gr.SelectData, history: List[Tuple[str, str]], logs: List[List[Dict[str, Any]]]):
            idx = evt.index if isinstance(evt.index, int) else None
            if idx is None or not logs or idx >= len(logs):
                return gr.update(value="<div class='relay-empty'>No relay for this turn.</div>")
            return gr.update(value=_render_log_html(logs[idx] or []))

        send.click(on_send, inputs=[msg, chat, relay_state], outputs=[msg, chat, relay_state])
        chat.select(on_select, inputs=[chat, relay_state], outputs=[relay_html])
    return demo

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    # 1) start A2A server in background
    def _serve():
        uvicorn.run(build_starlette_app(), host=CLAIMS_HOST, port=CLAIMS_PORT, log_level="info")
    threading.Thread(target=_serve, daemon=True).start()

    # 2) launch UI
    app = build_gradio()
    app.queue().launch(server_name="0.0.0.0", server_port=GRADIO_PORT, show_api=False)