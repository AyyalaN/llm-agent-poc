# a2a_relay_dashboard.py
# Gradio UI for inspecting A2A message relays (LangGraph-compatible agents)
# - Uses the A2A Python SDK (CardResolver + ClientFactory) for client creation
# - Streams events and renders a right-hand relay trace with a "messages-only" toggle
# - Left chat persists conversations by request; clicking a chat shows its relay trace
#
# Requirements:
#   pip install "a2a-sdk>=0.3.0" gradio httpx
#
# References for import/layout & streaming behavior:
#   - A2A SDK client creation with A2ACardResolver + ClientFactory  (Strands Agents doc)  [1]
#   - message/stream produces Message/TaskStatusUpdateEvent/TaskArtifactUpdateEvent       [2][3]
# [1]  [oai_citation:3‡Strands Agents](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/multi-agent/agent-to-agent/?utm_source=chatgpt.com)
# [2]  [oai_citation:4‡Google GitHub](https://google.github.io/A2A/tutorials/python/6-interact-with-server/?utm_source=chatgpt.com)
# [3]  [oai_citation:5‡Hugging Face](https://huggingface.co/blog/1bo/a2a-protocol-explained?utm_source=chatgpt.com)

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import gradio as gr
import httpx

# --- A2A SDK (current layout) ---
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory  # [1]
from a2a.types import (  # core protocol models
    Message,
    Role,
    Task,
    TaskStatusUpdateEvent,
    TaskArtifactUpdateEvent,
)
from a2a.utils.message import get_message_text  # helper to read text from Message parts


# ---------- Data models ----------
@dataclass
class AgentHandle:
    key: str                # "claims" | "mr"
    base_url: str
    color: str
    httpx_client: httpx.AsyncClient
    card: Any
    client: Any             # streaming client

@dataclass
class RelayEvent:
    idx: int
    from_agent: str         # "claims" | "mr"
    to_agent: str           # intended peer (for visualization)
    kind: str               # "message" | "status" | "artifact" | "task"
    text: str
    raw: Any = None
    meta: Dict[str, Any] | None = None

@dataclass
class RequestThread:
    request_id: str
    initiator: str          # "claims" or "mr"
    prompt: str
    chat_log: List[Tuple[str, str]] = field(default_factory=list)
    relay_events: List[RelayEvent] = field(default_factory=list)
    final_initiator_reply: Optional[str] = None

@dataclass
class AppState:
    agents: Dict[str, AgentHandle] = field(default_factory=dict)
    requests: Dict[str, RequestThread] = field(default_factory=dict)
    selected_request_id: Optional[str] = None


# ---------- Constants & helpers ----------
COLOR_MAP = {"claims": "#2563eb", "mr": "#059669"}
NAME_MAP = {"claims_agent": "claims", "mr_agent": "mr", "claims": "claims", "mr": "mr"}  # metadata → local keys
MAX_HOPS = 12


async def build_agent(key: str, base_url: str, color: str) -> AgentHandle:
    """
    Resolve the remote agent card and create a streaming client using the
    CardResolver + ClientFactory pattern.  (Matches current SDK examples.)  [1]
    """
    httpx_client = httpx.AsyncClient(timeout=None)
    resolver = A2ACardResolver(httpx_client=httpx_client, base_url=base_url)  # [1]
    card = await resolver.get_agent_card()
    factory = ClientFactory(ClientConfig(httpx_client=httpx_client, streaming=True))  # [1]
    client = factory.create(card)
    return AgentHandle(key=key, base_url=base_url, color=color, httpx_client=httpx_client, card=card, client=client)


async def close_agent(agent: AgentHandle):
    try:
        await agent.client.close()
    except Exception:
        pass
    try:
        await agent.httpx_client.aclose()
    except Exception:
        pass


def html_panel(text: str, color: str) -> str:
    return (
        f"<div style='border-left:6px solid {color};"
        f"padding:8px 10px; margin:6px 0; background:#f8f8f8; border-radius:8px;'>{gr.utils.sanitize_html(text)}</div>"
    )


def render_relay(events: List[RelayEvent], colors: Dict[str, str], messages_only: bool) -> str:
    filtered = [ev for ev in events if (ev.kind == "message" or not messages_only)]
    if not filtered:
        return "<i>No relayed messages for this request (with current filter).</i>"
    blocks = []
    for ev in filtered:
        head = f"<b>{ev.from_agent} → {ev.to_agent or '—'}</b> <small style='opacity:.7'>[{ev.kind}]</small>"
        body = gr.utils.sanitize_html(ev.text or "")
        blocks.append(f"<div style='margin-bottom:8px;'>{html_panel(head + '<br/>' + body, colors.get(ev.from_agent, '#999'))}</div>")
    return "<div>" + "".join(blocks) + "</div>"


def _truthy(v: Any, default: bool = True) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in {"", "0", "false", "no", "off", "never"}
    return default


def _build_text_message(text: str) -> Message:
    """
    Construct a text Message compatible with the SDK’s streaming send.
    The SDK accepts a Message with text parts; get_message_text() extracts it.  [2][3]
    """
    # Parts are commonly represented with "text" inside parts; Role.user identifies the sender.  [3]
    return Message(
        role=Role.user,
        parts=[{"text": text}],
        messageId=uuid4().hex,
    )


async def stream_once(client, speaker_key: str, next_peer_key: str, user_text: str) -> tuple[str, List[RelayEvent], bool, Dict[str, Any]]:
    """
    Send one message to `client` and collect streaming events until:
      - first agent Message arrives (we treat that as the baton handoff), OR
      - terminal TaskStatusUpdateEvent (completed/failed) arrives.
    Returns (reply_text, events, terminal, route_hint)
    """
    msg = _build_text_message(user_text)

    collected: List[RelayEvent] = []
    reply = ""
    terminal = False
    route_hint: Dict[str, Any] = {}

    async for item in client.send_message(msg):  # message/stream SSE producing Message/Task* events  [2][3]
        # Case A: An agent Message
        if isinstance(item, Message):
            reply = get_message_text(item) or ""
            meta = getattr(item, "metadata", None) or {}
            route_hint = {
                "relay": _truthy(meta.get("relay"), True),
                "audience": meta.get("audience"),
                "target_agent": meta.get("target_agent") or meta.get("delegateTo"),
            }
            collected.append(
                RelayEvent(
                    idx=len(collected),
                    from_agent=speaker_key,
                    to_agent=next_peer_key,
                    kind="message",
                    text=reply,
                    raw=item,
                    meta=route_hint or None,
                )
            )
            break  # treat first message as the baton we forward

        # Case B: A tuple (Task, Event) per streaming spec
        if isinstance(item, tuple) and len(item) == 2:
            task, ev = item
            if ev is None and isinstance(task, Task):
                collected.append(RelayEvent(
                    idx=len(collected), from_agent=speaker_key, to_agent=next_peer_key, kind="task",
                    text=f"task {getattr(task, 'id', '')} updated", raw=task
                ))
                continue

            if isinstance(ev, TaskStatusUpdateEvent):
                state = getattr(ev.status, "state", None)
                msgtxt = getattr(ev.status, "message", "") or ""
                is_final = bool(getattr(ev, "final", False))
                collected.append(RelayEvent(
                    idx=len(collected), from_agent=speaker_key, to_agent=next_peer_key, kind="status",
                    text=f"{state}: {msgtxt}", raw=ev
                ))
                # Stop at terminal (completed/failed)
                if is_final and str(state).lower() in {"completed", "taskstate.completed", "failed", "taskstate.failed"}:
                    terminal = True
                    break

            elif isinstance(ev, TaskArtifactUpdateEvent):
                # Show a short preview if it's a text artifact
                preview = ""
                try:
                    if ev.artifact and ev.artifact.parts:
                        part0 = ev.artifact.parts[0]
                        if isinstance(part0, dict):
                            preview = part0.get("text", "") or ""
                except Exception:
                    pass
                collected.append(RelayEvent(
                    idx=len(collected), from_agent=speaker_key, to_agent=next_peer_key, kind="artifact",
                    text=preview, raw=ev
                ))

    return reply, collected, terminal, route_hint


async def relay_by_default(app: AppState, thread: RequestThread) -> None:
    """
    Relay-by-default:
      - Start with the initiator agent; alternate between agents *unless* metadata
        includes target_agent or audience=user with relay=False, etc.
      - Append every hop’s events to thread.relay_events
      - Persist the final reply from the initiator in left chat
    """
    a = app.agents[thread.initiator]
    # Choose the other as the default next hop
    other_key = next(k for k in app.agents if k != thread.initiator)
    b = app.agents[other_key]

    current = a
    next_peer = b
    baton = thread.prompt

    for _ in range(MAX_HOPS):
        reply, events, terminal, hint = await stream_once(current.client, current.key, next_peer.key, baton)
        for ev in events:
            ev.to_agent = next_peer.key  # for the visualization
        thread.relay_events.extend(events)

        # capture first response from initiator to show in left chat
        if reply and current.key == thread.initiator and thread.final_initiator_reply is None:
            thread.final_initiator_reply = reply

        if terminal:
            break

        # Audience & relay rules:
        # - If audience=user and relay=False (explicit), stop forwarding.
        # - Otherwise, relay=True by default (peer) per our demo contract.
        relay_ok = True
        if hint:
            if hint.get("audience") == "user":
                relay_ok = _truthy(hint.get("relay"), False)
            else:
                relay_ok = _truthy(hint.get("relay"), True)
        if not relay_ok or not reply:
            break

        baton = reply

        # Metadata-directed routing to a named agent
        next_key = None
        tgt = (hint.get("target_agent") or "").strip().lower()
        if tgt:
            next_key = NAME_MAP.get(tgt)
        if next_key and next_key in app.agents:
            current = app.agents[next_key]
            other_key = next(k for k in app.agents if k != next_key)
            next_peer = app.agents[other_key]
        else:
            current, next_peer = next_peer, current  # flip-flop by default


# ---------- Gradio handlers ----------
async def on_connect(claims_base: str, mr_base: str, app: AppState):
    # Tear down any previous clients
    for handle in list(app.agents.values()):
        await close_agent(handle)
    app.agents.clear()

    claims = await build_agent("claims", claims_base, COLOR_MAP["claims"])
    mr = await build_agent("mr", mr_base, COLOR_MAP["mr"])
    app.agents = {"claims": claims, "mr": mr}
    return gr.update(value="Connected ✓"), gr.update(choices=["claims", "mr"], value="claims")


async def on_submit(prompt: str, initiator: str, app: AppState,
                    chat: List[Tuple[str, str]], relay_html: str, messages_only: bool):
    if not prompt.strip():
        return gr.update(), gr.update(), gr.update()

    rid = uuid4().hex
    thread = RequestThread(request_id=rid, initiator=initiator, prompt=prompt)
    app.requests[rid] = thread
    app.selected_request_id = rid

    # left chat: add placeholder assistant bubble
    chat = chat + [(prompt, "")]
    await relay_by_default(app, thread)
    chat[-1] = (prompt, thread.final_initiator_reply or "")

    relay_html = render_relay(thread.relay_events, COLOR_MAP, messages_only=messages_only)
    return chat, relay_html, rid


def on_select_chat(evt: gr.SelectData, app: AppState, chat: List[Tuple[str, str]], messages_only: bool):
    # Gradio passes selected (user, assistant) tuple as evt.value
    try:
        selected_user_text = evt.value[0]
    except Exception:
        return gr.update()

    chosen = None
    for r in app.requests.values():
        if r.prompt == selected_user_text:
            chosen = r
            break
    if not chosen:
        return gr.update()

    app.selected_request_id = chosen.request_id
    return gr.update(value=render_relay(chosen.relay_events, COLOR_MAP, messages_only=messages_only))


def on_toggle_messages_only(messages_only: bool, app: AppState):
    rid = app.selected_request_id
    if not rid or rid not in app.requests:
        return gr.update()
    thread = app.requests[rid]
    return gr.update(value=render_relay(thread.relay_events, COLOR_MAP, messages_only=messages_only))


# ---------- Build UI ----------
def build_ui():
    with gr.Blocks(css="""
    .left-col {min-height: 560px;}
    .right-col {min-height: 560px; background: #fff;}
    """) as demo:
        app_state = gr.State(AppState())

        with gr.Row():
            gr.Markdown("### A2A Relay Dashboard (SDK client, metadata-aware, messages-only filter)")

        with gr.Row():
            claims_url = gr.Textbox(label="Claims Agent Base URL", value=os.getenv("CLAIMS_BASE_URL", "http://localhost:10000"))
            mr_url = gr.Textbox(label="MR Agent Base URL", value=os.getenv("MR_BASE_URL", "http://localhost:10001"))
            connect_btn = gr.Button("Connect")
            status = gr.Markdown("")

        with gr.Row():
            with gr.Column(scale=1, elem_classes="left-col"):
                initiator = gr.Dropdown(choices=["claims", "mr"], value="claims", label="Initiator Agent")
                chat = gr.Chatbot(label="Chat with Initiator", height=500, type="tuple")
                prompt = gr.Textbox(label="Your prompt", placeholder="e.g., What's the status of C-2002?")
                send = gr.Button("Send")

            with gr.Column(scale=1, elem_classes="right-col"):
                with gr.Row():
                    gr.Markdown("**Inter-Agent Relay Trace (for selected request)**")
                    messages_only = gr.Checkbox(value=True, label="Messages only (hide status/artifact)")
                relay_view = gr.HTML("<i>Connect and send a prompt to see relay traffic…</i>", height=500)
                selected_request = gr.Textbox(visible=False)

        # Wire events
        connect_btn.click(on_connect, [claims_url, mr_url, app_state], [status, initiator])
        send.click(on_submit, [prompt, initiator, app_state, chat, relay_view, messages_only], [chat, relay_view, selected_request])
        chat.select(on_select_chat, [app_state, chat, messages_only], [relay_view])
        messages_only.change(on_toggle_messages_only, [messages_only, app_state], [relay_view])

    return demo


if __name__ == "__main__":
    ui = build_ui()
    ui.queue().launch()