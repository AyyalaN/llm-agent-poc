"""
Gradio UI to visualize two A2A agents talking.
- Connects to two A2A servers (Agent A / Agent B)
- Streams via /v1/message:stream (SSE)
- Optionally auto-relays messages between them (bounded hop count)
- Shows labeled transcript + raw JSON events + export

Requires: pip install gradio requests
"""

import base64, json, time, uuid, threading
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional, Tuple

import gradio as gr
import requests

# ---------- A2A client primitives (protocol-level, spec-compliant) ----------

def basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"

def build_headers(username: str, password: str, extra_headers_json: str = "") -> Dict[str, str]:
    headers = {
        "Authorization": basic_auth_header(username, password),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if extra_headers_json.strip():
        try:
            extra = json.loads(extra_headers_json)
            if not isinstance(extra, dict):
                raise ValueError("Extra headers must be a JSON object.")
            headers.update({str(k): str(v) for k, v in extra.items()})
        except Exception as e:
            raise ValueError(f"Invalid extra headers JSON: {e}")
    return headers

def fetch_agent_card(base_url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    # Spec maps GET /v1/card for REST transport. (Also exposed via JSON-RPC/gRPC.) 
    # Ref: A2A spec “agent/getAuthenticatedExtendedCard” & method mappings. 
    # We use public card (no body) for label purposes. 
    url = base_url.rstrip("/") + "/v1/card"
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def sse_stream(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Generator[Dict[str, Any], None, None]:
    """
    POST to /v1/message:stream and iterate SSE "data:" frames.
    Each SSE data is a JSON-RPC response per spec; payload in `result`.
    """
    with requests.post(url, headers=headers, json=payload, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        buf = []
        for raw in resp.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = raw.strip()
            if not line:
                # blank line terminates one SSE event -> parse accumulated 'data:' lines
                data_lines = [l[5:].strip() for l in buf if l.startswith("data:")]
                if data_lines:
                    try:
                        # Some servers may split JSON across multiple 'data:' lines; join.
                        obj = json.loads("\n".join(data_lines))
                        yield obj
                    except Exception as _:
                        pass
                buf.clear()
                continue
            # Accumulate SSE event lines until blank-line separator
            buf.append(line)

def extract_text_parts(message: Dict[str, Any]) -> str:
    """Concatenate human-readable text from A2A Message.parts (only 'text' parts)."""
    parts = message.get("parts", []) or []
    out = []
    for p in parts:
        if p.get("kind") == "text":
            out.append(p.get("text", ""))
        elif p.get("kind") in ("file", "data"):
            # Keep it short for UI; you could render file/data details if you like.
            out.append(f"[{p.get('kind')}]")
    return "\n".join([s for s in out if s])

@dataclass
class AgentConfig:
    label: str
    base_url: str
    username: str
    password: str
    extra_headers_json: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    card: Dict[str, Any] = field(default_factory=dict)
    # last known context/task if we continue a thread:
    context_id: Optional[str] = None
    last_task_id: Optional[str] = None

@dataclass
class TranscriptEvent:
    t: float
    origin: str      # "A" or "B" or "system"
    kind: str        # "message" | "status" | "artifact" | "error"
    role: Optional[str]  # "user" | "agent" | None
    text: str
    raw: Dict[str, Any]

# ---------- Orchestration logic ----------

def send_stream_message(agent: AgentConfig, message: Dict[str, Any], history_len: int = 6) -> Generator[Dict[str, Any], None, None]:
    """
    Send a message and stream updates from this agent.
    Message structure mirrors the A2A Message object; we always include a messageId.
    """
    url = agent.base_url.rstrip("/") + "/v1/message:stream"
    payload = {
        "message": {
            **message,
            "messageId": message.get("messageId") or str(uuid.uuid4()),
        },
        "configuration": {
            "historyLength": history_len
        }
    }
    yield from sse_stream(url, payload, agent.headers)

def parse_stream_result(result_obj: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """
    Identify which A2A result we got inside JSON-RPC 'result' field:
    Could be Message | Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent.
    Return ('type', payload)
    """
    result = result_obj.get("result", {})
    if not result:
        return ("unknown", result_obj)

    # Heuristics based on spec object shapes
    if "role" in result and "parts" in result:
        return ("message", result)  # Message
    if "status" in result and "id" in result and result.get("kind") == "task":
        return ("task", result)     # Initial Task
    if "status" in result and "taskId" in result and result.get("kind") == "task-status-update":
        return ("statusUpdate", result)
    if "taskId" in result and result.get("kind") == "task-artifact-update":
        return ("artifactUpdate", result)

    # Some servers wrap as {message?:, task?:, statusUpdate?:, artifactUpdate?:}
    for k in ("message", "task", "statusUpdate", "artifactUpdate"):
        if k in result:
            return (k, result[k])

    return ("unknown", result)

def auto_relay_conversation(
    initiator: AgentConfig,
    responder: AgentConfig,
    user_prompt: str,
    hop_limit: int = 6,
    max_wait_idle_s: int = 8,
) -> Tuple[List[TranscriptEvent], Dict[str, Any], Dict[str, Any]]:
    """
    Start with initiator, relay messages back-and-forth up to hop_limit.
    We always send downstream messages with role="user" (client to server).
    """
    events: List[TranscriptEvent] = []

    def record(origin, kind, role, text, raw):
        events.append(TranscriptEvent(time.time(), origin, kind, role, text, raw))

    # Seed: send the user prompt to initiator
    msg = {"role": "user", "parts": [{"kind": "text", "text": user_prompt}]}
    hops = 0
    current_sender = initiator
    current_receiver = responder
    last_text_from_sender = None

    while hops < hop_limit:
        try:
            for frame in send_stream_message(current_sender, msg):
                typ, payload = parse_stream_result(frame)
                if typ == "message":
                    role = payload.get("role")
                    text = extract_text_parts(payload).strip()
                    record(current_sender.label, "message", role, text or "[non-text parts]", frame)
                    # Keep context/task if returned on messages (optional)
                    if payload.get("contextId"):
                        current_sender.context_id = payload["contextId"]
                    last_text_from_sender = text

                elif typ == "task":
                    # initial task snapshot with status + maybe history
                    status = payload.get("status", {}).get("state")
                    record(current_sender.label, "status", None, f"task {payload.get('id')} -> {status}", frame)
                    current_sender.last_task_id = payload.get("id")

                elif typ == "statusUpdate":
                    st = payload.get("status", {}).get("state")
                    msg_txt = payload.get("status", {}).get("message", "")
                    record(current_sender.label, "status", None, f"{st} {('- ' + msg_txt) if msg_txt else ''}".strip(), frame)

                elif typ == "artifactUpdate":
                    record(current_sender.label, "artifact", None, f"artifact update for task {payload.get('taskId')}", frame)

                # We can impose idle timeout if no 'message' arrives for a bit
            # streaming POST finished (task terminal or server ended stream)
        except Exception as e:
            record("system", "error", None, f"{current_sender.label} stream error: {e}", {"exception": str(e)})
            break

        hops += 1

        # If sender produced a (non-empty) message as an agent, relay to the other side as user.
        if last_text_from_sender:
            relay_text = last_text_from_sender
            msg = {"role": "user", "parts": [{"kind": "text", "text": relay_text}]}
            # flip roles
            current_sender, current_receiver = current_receiver, current_sender
            last_text_from_sender = None
            continue
        else:
            # No relayable text -> end conversation
            break

    return events, {"a": initiator.card}, {"b": responder.card}

# ---------- Gradio UI wiring ----------

def on_connect(a_url, a_user, a_pass, a_extra, b_url, b_user, b_pass, b_extra):
    # Build configs and fetch cards
    a = AgentConfig("A", a_url, a_user, a_pass, a_extra)
    b = AgentConfig("B", b_url, b_user, b_pass, b_extra)
    a.headers = build_headers(a.username, a.password, a.extra_headers_json)
    b.headers = build_headers(b.username, b.password, b.extra_headers_json)
    a.card = fetch_agent_card(a.base_url, a.headers)
    b.card = fetch_agent_card(b.base_url, b.headers)
    a_name = a.card.get("name") or a.card.get("title") or "Agent A"
    b_name = b.card.get("name") or b.card.get("title") or "Agent B"
    return (json.dumps(a.card, indent=2), json.dumps(b.card, indent=2),
            f"Connected ✓  A = {a_name}", f"Connected ✓  B = {b_name}",
            json.dumps({"A": a.card, "B": b.card}, indent=2))

def run_demo(a_url, a_user, a_pass, a_extra, b_url, b_user, b_pass, b_extra, initiator_choice, prompt, hop_limit):
    # Prepare agents
    a = AgentConfig("A", a_url, a_user, a_pass, a_extra)
    b = AgentConfig("B", b_url, b_user, b_pass, b_extra)
    a.headers = build_headers(a.username, a.password, a.extra_headers_json)
    b.headers = build_headers(b.username, b.password, b.extra_headers_json)
    a.card = fetch_agent_card(a.base_url, a.headers)
    b.card = fetch_agent_card(b.base_url, b.headers)

    initiator = a if initiator_choice == "A" else b
    responder = b if initiator_choice == "A" else a

    # Run
    events, a_meta, b_meta = auto_relay_conversation(initiator, responder, prompt, hop_limit=hop_limit)

    # Render a simple HTML transcript with badges
    def badge(label): 
        return f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;background:{"#2563eb" if label=="A" else "#16a34a"};color:white;font-size:12px;">Agent {label}</span>'
    def kind_style(k):
        return {"message": "", "status": "opacity:0.7;font-style:italic;", "artifact": "opacity:0.8;", "error":"color:#b91c1c;font-weight:600;"}.get(k,"")
    rows = []
    for e in events:
        time_str = time.strftime("%H:%M:%S", time.localtime(e.t))
        safe_text = (e.text or "").replace("<","&lt;").replace(">","&gt;")
        role = f" ({e.role})" if e.role else ""
        rows.append(f'<div style="margin:8px 0;"><span style="opacity:0.65">{time_str}</span> {badge(e.origin)} '
                    f'<span style="opacity:0.6;">[{e.kind}{role}]</span>'
                    f'<div style="margin:4px 0 0 24px;{kind_style(e.kind)};white-space:pre-wrap;">{safe_text}</div></div>')
    html = "<div style='font-family:ui-sans-serif'>" + "\n".join(rows) + "</div>"

    raw = [dict(t=e.t, origin=e.origin, kind=e.kind, role=e.role, text=e.text, raw=e.raw) for e in events]
    export = json.dumps({"meta":{"A":a_meta["a"],"B":b_meta["b"]},"events":raw}, indent=2)
    return html, export

with gr.Blocks(title="A2A Dual-Agent Viewer") as demo:
    gr.Markdown("### Agent2Agent (A2A) Dual-Agent Viewer · Gradio\nProvide two A2A endpoints and credentials. Connect, then run a short demo that relays messages between them.")
    with gr.Row():
        with gr.Column():
            gr.Markdown("**Agent A**")
            a_url = gr.Textbox(label="Base URL (e.g., http://localhost:5555)", placeholder="http(s)://host:port", scale=2)
            a_user = gr.Textbox(label="Basic-Auth Username", value="", type="text")
            a_pass = gr.Textbox(label="Basic-Auth Password", value="", type="password")
            a_extra = gr.Textbox(label="Extra headers (JSON)", placeholder='{"X-API-Key":"..."}')
            a_card = gr.Code(label="Agent A Card (GET /v1/card)", interactive=False)
            a_status = gr.Markdown("")
        with gr.Column():
            gr.Markdown("**Agent B**")
            b_url = gr.Textbox(label="Base URL (e.g., http://localhost:6666)", placeholder="http(s)://host:port", scale=2)
            b_user = gr.Textbox(label="Basic-Auth Username", value="", type="text")
            b_pass = gr.Textbox(label="Basic-Auth Password", value="", type="password")
            b_extra = gr.Textbox(label="Extra headers (JSON)", placeholder='{"X-API-Key":"..."}')
            b_card = gr.Code(label="Agent B Card (GET /v1/card)", interactive=False)
            b_status = gr.Markdown("")

    connect = gr.Button("Connect & Fetch Cards")
    cards_bundle = gr.Code(label="Cards Bundle (JSON)", interactive=False)

    connect.click(
        fn=on_connect,
        inputs=[a_url, a_user, a_pass, a_extra, b_url, b_user, b_pass, b_extra],
        outputs=[a_card, b_card, a_status, b_status, cards_bundle],
    )

    gr.Markdown("---")
    with gr.Row():
        initiator_choice = gr.Radio(choices=["A","B"], value="A", label="Initiate conversation from")
        hop_limit = gr.Slider(1, 20, value=6, step=1, label="Hop limit (to avoid infinite loops)")
    prompt = gr.Textbox(label="Initial prompt", value="Summarize this and hand off one key insight to the other agent.", lines=2)
    run = gr.Button("Run Auto-Relay Demo")
    transcript = gr.HTML(label="Transcript", elem_id="transcript")
    export = gr.Code(label="Export (JSON transcript)")

    run.click(
        fn=run_demo,
        inputs=[a_url, a_user, a_pass, a_extra, b_url, b_user, b_pass, b_extra, initiator_choice, prompt, hop_limit],
        outputs=[transcript, export],
    )

if __name__ == "__main__":
    demo.launch()
