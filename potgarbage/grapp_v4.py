# a2a_relay_dashboard.py
import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple, Optional
from uuid import uuid4

import gradio as gr
import httpx

# --- A2A SDK imports (official) ---
from a2a.client import A2ACardResolver  # card discovery
from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
# (Optionally, you can switch to the legacy class)
# from a2a.client import A2AClient as LegacyA2AClient  # legacy path

from a2a import (
    Role,
    Message,
    Task,
    TaskStatusUpdateEvent,
    TaskArtifactUpdateEvent,
    # TaskState enum is exposed via FromProto.task_state()/statuses; we check by name
)
from a2a.utils.message import get_message_text, new_agent_text_message  # helpers


# -----------------------------
# App State Structures
# -----------------------------
@dataclass
class AgentHandle:
    key: str
    base_url: str
    color: str
    httpx_client: httpx.AsyncClient
    card: Any     # a2a.types.AgentCard
    client: Any   # a2a.client.client.Client (streaming aware)

@dataclass
class RelayEvent:
    idx: int
    from_agent: str
    to_agent: str
    kind: str            # "message" | "status" | "artifact" | "task"
    text: str
    raw: Any = None

@dataclass
class RequestThread:
    request_id: str
    initiator: str
    prompt: str
    chat_log: List[Tuple[str, str]] = field(default_factory=list)   # [(user, agent_response)]
    relay_events: List[RelayEvent] = field(default_factory=list)

@dataclass
class AppState:
    agents: Dict[str, AgentHandle] = field(default_factory=dict)
    # request_id -> thread
    requests: Dict[str, RequestThread] = field(default_factory=dict)
    # UI selections
    selected_request_id: Optional[str] = None


# -----------------------------
# A2A Client setup
# -----------------------------
async def build_agent(key: str, base_url: str, color: str) -> AgentHandle:
    httpx_client = httpx.AsyncClient(timeout=None)
    resolver = A2ACardResolver(httpx_client=httpx_client, base_url=base_url)
    card = await resolver.get_agent_card()  # fetches /.well-known/agent-card.json
    # Modern Client API via ClientFactory (handles JSON-RPC/REST/GRPC based on card)
    config = ClientConfig(streaming=True, httpx_client=httpx_client)
    factory = ClientFactory(config=config)
    client = await factory.create(card)
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


# -----------------------------
# Relay logic (relay-by-default)
# -----------------------------
MAX_HOPS = 12

def html_color_span(text: str, color: str) -> str:
    return f'<div style="border-left:6px solid {color}; padding:8px 10px; margin:6px 0; background:#f8f8f8; border-radius:8px;">{gr.utils.sanitize_html(text)}</div>'

def render_relay(events: List[RelayEvent], color_map: Dict[str, str]) -> str:
    if not events:
        return "<i>No relayed messages yet for this request.</i>"
    blocks = []
    for ev in events:
        head = f"<b>{ev.from_agent} → {ev.to_agent}</b> <small style='opacity:.7'>[{ev.kind}]</small>"
        body = gr.utils.sanitize_html(ev.text or "")
        blocks.append(f'<div style="margin-bottom:8px;">{html_color_span(head + "<br/>" + body, color_map.get(ev.from_agent, "#999"))}</div>')
    return "<div>" + "".join(blocks) + "</div>"

async def stream_once_and_collect(agent: AgentHandle, user_text: str) -> Tuple[str, List[RelayEvent], bool]:
    """
    Send a single user->agent turn and collect until we
    - see a Message (we'll relay that onward), or
    - see a terminal Task state (completed/failed) -> stop relaying.

    Returns: (agent_reply_text, events, terminal)
    """
    # Build a user message (SDK accepts Message objects on the modern client)
    msg = Message(role=Role.user, parts=[{"kind": "text", "text": user_text}], messageId=uuid4().hex)

    collected: List[RelayEvent] = []
    terminal = False
    agent_reply = ""

    # The modern client returns an async iterator of:
    # - (Task, TaskStatusUpdateEvent|TaskArtifactUpdateEvent|None) tuples, or
    # - Message (final)
    async for ev in agent.client.send_message(msg):
        # Message (agent reply or clarification)
        if isinstance(ev, Message):
            agent_reply = get_message_text(ev) or ""
            collected.append(RelayEvent(
                idx=len(collected), from_agent=agent.key, to_agent="", kind="message", text=agent_reply, raw=ev
            ))
            # We break here so the caller can relay this to the peer.
            break

        # Tuple: (Task, Update?) for streaming / task lifecycle
        if isinstance(ev, tuple) and len(ev) == 2:
            task, update = ev
            if isinstance(update, TaskStatusUpdateEvent):
                # Detect terminal states, per tutorial & TaskState docs
                state_name = getattr(update.status, "state", None)
                is_final = bool(getattr(update, "final", False))
                text = getattr(update.status, "message", "") or ""
                collected.append(RelayEvent(
                    idx=len(collected), from_agent=agent.key, to_agent="", kind="status", text=f"{state_name}: {text}", raw=update
                ))
                if is_final and str(state_name).lower() in {"taskstate.completed", "completed", "taskstate.failed", "failed"}:
                    terminal = True
                    break
            elif isinstance(update, TaskArtifactUpdateEvent):
                # Final answer often arrives as an artifact; append preview
                preview = ""
                try:
                    # parts contain text/data; we keep this simple
                    if update.artifact and update.artifact.parts:
                        maybe_text = update.artifact.parts[0]
                        if isinstance(maybe_text, dict):
                            preview = maybe_text.get("text", "") or ""
                except Exception:
                    pass
                collected.append(RelayEvent(
                    idx=len(collected), from_agent=agent.key, to_agent="", kind="artifact", text=preview, raw=update
                ))
            elif update is None and isinstance(task, Task):
                # First tuple sometimes carries just Task snapshot
                collected.append(RelayEvent(
                    idx=len(collected), from_agent=agent.key, to_agent="", kind="task", text=f"task {task.id} updated", raw=task
                ))

    return agent_reply, collected, terminal

async def relay_by_default(state: AppState, request: RequestThread) -> None:
    """
    Alternates between initiator and the peer, forwarding the latest agent reply
    as the next 'user' message to the other agent, until terminal.
    """
    a = state.agents[request.initiator]
    bkey = next(k for k in state.agents.keys() if k != request.initiator)
    b = state.agents[bkey]

    current_speaker = a   # whose client we call next
    next_peer = b
    user_in = request.prompt

    for hop in range(MAX_HOPS):
        reply, events, terminal = await stream_once_and_collect(current_speaker, user_in)
        # tag the collected events with to_agent for UI
        for ev in events:
            ev.to_agent = next_peer.key
        request.relay_events.extend(events)

        # If we got a direct agent Message, also persist to the left-side chat
        # We append only when the reply came from the *initiator*, to keep a simple user ↔ initiator chat
        if reply and current_speaker.key == request.initiator:
            request.chat_log.append((request.prompt if hop == 0 else "", reply))

        if terminal:
            break

        # Convention to stop relaying if agent asks us not to:
        # If last event has metadata.relay == "never", stop.
        try:
            last = events[-1].raw
            meta = getattr(last, "metadata", None)
            if isinstance(meta, dict) and str(meta.get("relay", "")).lower() == "never":
                break
        except Exception:
            pass

        # Prepare to forward this reply (as a *user* message) to the peer
        if not reply:
            break  # nothing to relay
        user_in = reply
        # swap speaker/peer
        current_speaker, next_peer = next_peer, current_speaker


# -----------------------------
# Gradio UI + Handlers
# -----------------------------
COLOR_MAP = {"claims": "#2563eb", "mr": "#059669"}  # blue / green

async def on_connect(claims_base: str, mr_base: str, app_state: AppState):
    # build agents (or rebuild)
    # close old
    for h in list(app_state.agents.values()):
        await close_agent(h)
    app_state.agents.clear()

    claims = await build_agent("claims", claims_base, COLOR_MAP["claims"])
    mr = await build_agent("mr", mr_base, COLOR_MAP["mr"])
    app_state.agents = {"claims": claims, "mr": mr}

    return gr.update(value="Connected ✓"), gr.update(choices=["claims", "mr"], value="claims")

async def on_submit(prompt: str, initiator: str, app_state: AppState, chat: List[Tuple[str, str]], relay_html: str):
    if not prompt.strip():
        return gr.update(), gr.update(), gr.update()

    request_id = uuid4().hex
    thread = RequestThread(request_id=request_id, initiator=initiator, prompt=prompt)
    app_state.requests[request_id] = thread
    app_state.selected_request_id = request_id

    # left: add the user's message
    chat = chat + [(prompt, "")]

    await relay_by_default(app_state, thread)

    # fill the last assistant bubble in the chat (initiator’s reply)
    if thread.chat_log:
        # find the last empty assistant slot (the pair we just pushed)
        chat[-1] = (prompt, thread.chat_log[-1][1])

    relay_html = render_relay(thread.relay_events, COLOR_MAP)
    return chat, relay_html, request_id

def on_select_chat(evt: gr.SelectData, app_state: AppState, chat: List[Tuple[str, str]]):
    """
    When user clicks a *user* message in the left chat, select the corresponding request.
    We store threads in insertion order; here we match by the user's text.
    """
    selected_user_text = ""
    try:
        # evt.value is (user, assistant) tuple; pick user text
        selected_user_text = evt.value[0]
    except Exception:
        pass

    # naive match: find the last thread with this prompt
    chosen = None
    for r in app_state.requests.values():
        if r.prompt == selected_user_text:
            chosen = r
    if not chosen:
        return gr.update()  # no change

    app_state.selected_request_id = chosen.request_id
    return gr.update(value=render_relay(chosen.relay_events, COLOR_MAP))

def build_ui():
    with gr.Blocks(css="""
    .left-col {min-height: 560px;}
    .right-col {min-height: 560px; background: #fff;}
    """) as demo:
        app_state = gr.State(AppState())

        with gr.Row():
            gr.Markdown("### A2A Relay Dashboard (LangGraph x A2A)")

        with gr.Row():
            claims_url = gr.Textbox(label="Claims Agent Base URL", value=os.getenv("CLAIMS_BASE_URL", "http://localhost:11000"))
            mr_url = gr.Textbox(label="MR Agent Base URL", value=os.getenv("MR_BASE_URL", "http://localhost:12000"))
            connect_btn = gr.Button("Connect")
            status = gr.Markdown("", elem_id="status")

        with gr.Row():
            with gr.Column(scale=1, elem_classes="left-col"):
                initiator = gr.Dropdown(choices=["claims", "mr"], value="claims", label="Initiator Agent")
                chat = gr.Chatbot(label="Chat with Initiator", height=500, type="tuple")
                prompt = gr.Textbox(label="Your prompt", placeholder="e.g., What's the status of claim 12345?")
                send = gr.Button("Send")

            with gr.Column(scale=1, elem_classes="right-col"):
                gr.Markdown("**Inter-Agent Relay Trace (for selected request)**")
                relay_view = gr.HTML("<i>Connect and send a prompt to see relay traffic…</i>", height=500)
                selected_request = gr.Textbox(visible=False)

        # wire up events
        connect_btn.click(on_connect, [claims_url, mr_url, app_state], [status, initiator])
        send.click(on_submit, [prompt, initiator, app_state, chat, relay_view], [chat, relay_view, selected_request])
        # clicking messages in Chatbot (Gradio supports select on Chatbot items)
        chat.select(on_select_chat, [app_state, chat], [relay_view])

    return demo


if __name__ == "__main__":
    demo = build_ui()
    demo.queue().launch()