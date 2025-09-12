"""
Gradio UI to visualize two A2A agents talking with RELAY-BY-DEFAULT policy.

- If an agent emits a Message, we RELAY it to the other agent by default.
- Agents can opt OUT per-turn with message.metadata.relay in {"never", "no", False}
  or message.metadata.doNotRelay == True.
- If message.metadata.delegateTo is "A" or "B", we honor that as the next hop target.
- We stop streaming on Task terminal states or when a statusUpdate has final: true.
- A hop_limit prevents infinite loops.

Requires: pip install gradio requests
"""

import base64, json, time, uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional, Tuple

import gradio as gr
import requests


# ---------- A2A helpers ----------

TERMINAL_STATES = {"completed", "canceled", "rejected", "failed"}
RELAY_BY_DEFAULT = True  # <-- policy switch you asked for

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
        extra = json.loads(extra_headers_json)
        if not isinstance(extra, dict):
            raise ValueError("Extra headers must be a JSON object.")
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers

def fetch_agent_card(base_url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    url = base_url.rstrip("/") + "/v1/card"
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def sse_stream(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Generator[Dict[str, Any], None, None]:
    with requests.post(url, headers=headers, json=payload, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        buf = []
        for raw in resp.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = (raw or "").strip()
            if not line:
                data_lines = [l[5:].strip() for l in buf if l.startswith("data:")]
                if data_lines:
                    try:
                        obj = json.loads("\n".join(data_lines))
                        yield obj
                    except Exception:
                        pass
                buf.clear()
                continue
            buf.append(line)

def extract_text_parts(message: Dict[str, Any]) -> str:
    parts = message.get("parts", []) or []
    out = []
    for p in parts:
        kind = p.get("kind")
        if kind == "text":
            out.append(p.get("text", ""))
        elif kind in ("file", "data"):
            out.append(f"[{kind}]")
    return "\n".join([s for s in out if s])


# ---------- Data classes ----------

@dataclass
class AgentConfig:
    label: str
    base_url: str
    username: str
    password: str
    extra_headers_json: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    card: Dict[str, Any] = field(default_factory=dict)
    context_id: Optional[str] = None
    last_task_id: Optional[str] = None

@dataclass
class TranscriptEvent:
    t: float
    origin: str      # "A" | "B" | "system"
    kind: str        # "message" | "status" | "artifact" | "error"
    role: Optional[str]
    text: str
    raw: Dict[str, Any]


# ---------- Streaming & parsing ----------

def send_stream_message(agent: AgentConfig, message: Dict[str, Any], history_len: int = 6) -> Generator[Dict[str, Any], None, None]:
    url = agent.base_url.rstrip("/") + "/v1/message:stream"
    payload = {
        "message": {**message, "messageId": message.get("messageId") or str(uuid.uuid4())},
        "configuration": {"historyLength": history_len},
    }
    yield from sse_stream(url, payload, agent.headers)

def parse_stream_result(result_obj: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    result = result_obj.get("result", {}) or {}
    if not result:
        return ("unknown", result_obj)

    # direct shapes
    if "role" in result and "parts" in result:
        return ("message", result)  # Message
    if result.get("kind") == "task" and "status" in result and "id" in result:
        return ("task", result)
    if result.get("kind") == "task-status-update" and "status" in result and "taskId" in result:
        return ("statusUpdate", result)
    if result.get("kind") == "task-artifact-update" and "taskId" in result:
        return ("artifactUpdate", result)

    # wrapped variants
    for k in ("message", "task", "statusUpdate", "artifactUpdate"):
        if k in result:
            return (k, result[k])

    return ("unknown", result)


# ---------- Relay-by-default orchestration ----------

def auto_relay_conversation(
    agent_a: AgentConfig,
    agent_b: AgentConfig,
    initiator_label: str,
    user_prompt: str,
    hop_limit: int = 6,
) -> Tuple[List[TranscriptEvent], Dict[str, Any], Dict[str, Any]]:
    """
    Relay-by-default policy:
      - If an agent emits a Message with text, we plan to relay that text to the next hop,
        UNLESS metadata disables relaying.
      - delegateTo: "A" | "B" (optional) sets the explicit next hop target.
      - We stop the stream on terminal states or statusUpdate.final == true.
      - We stop the whole ping-pong when hop_limit is reached or there's nothing to relay.
    """

    events: List[TranscriptEvent] = []
    def record(origin, kind, role, text, raw):
        events.append(TranscriptEvent(time.time(), origin, kind, role, text, raw))

    # Map labels to agents once
    agents = {"A": agent_a, "B": agent_b}

    # Initialize roles
    current_label = initiator_label  # "A" or "B"
    other_label = "B" if current_label == "A" else "A"

    # Seed message from user to the initiator
    next_message = {"role": "user", "parts": [{"kind": "text", "text": user_prompt}]}

    hops = 0
    while hops < hop_limit and next_message is not None:
        sender = agents[current_label]
        receiver = agents[other_label]

        # Track what to do AFTER the stream ends for this hop
        relay_text: Optional[str] = None
        relay_allowed: bool = RELAY_BY_DEFAULT
        explicit_target: Optional[str] = None
        stream_reached_terminal = False

        try:
            for frame in send_stream_message(sender, next_message):
                typ, payload = parse_stream_result(frame)

                if typ == "message":
                    role = payload.get("role")
                    text = extract_text_parts(payload).strip()
                    record(sender.label, "message", role, text or "[non-text parts]", frame)

                    # capture context if present
                    if payload.get("contextId"):
                        sender.context_id = payload["contextId"]

                    # Relay-by-default, unless metadata opts out for this turn
                    meta = (payload.get("metadata") or {})
                    if isinstance(meta, dict):
                        # opt-out switches
                        if str(meta.get("relay")).lower() in {"never", "no", "false"} or bool(meta.get("doNotRelay")):
                            relay_allowed = False
                        # optional directed hop
                        if meta.get("delegateTo") in {"A", "B"}:
                            explicit_target = meta["delegateTo"]

                    # we will relay the *last* non-empty text message seen this hop
                    if text:
                        relay_text = text

                elif typ == "task":
                    st = (payload.get("status") or {}).get("state", "")
                    record(sender.label, "status", None, f"task {payload.get('id')} -> {st}", frame)
                    sender.last_task_id = payload.get("id")

                elif typ == "statusUpdate":
                    status = (payload.get("status") or {}).get("state", "")
                    is_final = bool(payload.get("final"))
                    msg_txt = (payload.get("status") or {}).get("message", "")
                    record(sender.label, "status", None, f"{status}{(' - ' + msg_txt) if msg_txt else ''}", frame)
                    if is_final or status in TERMINAL_STATES:
                        stream_reached_terminal = True

                elif typ == "artifactUpdate":
                    record(sender.label, "artifact", None, f"artifact update for task {payload.get('taskId')}", frame)

                # unknown types are ignored but kept in raw export
        except Exception as e:
            record("system", "error", None, f"{sender.label} stream error: {e}", {"exception": str(e)})
            # on error, don't attempt a relay for this hop
            relay_text = None
            relay_allowed = False
            stream_reached_terminal = True

        # hop finished (server closed stream or terminal reached)
        hops += 1

        # Decide whether to relay
        if relay_allowed and relay_text:
            # choose next target
            if explicit_target in {"A", "B"} and explicit_target in agents:
                # honor explicit delegateTo
                next_target_label = explicit_target
            else:
                # ping-pong by default
                next_target_label = other_label

            # Prepare next user message
            next_message = {"role": "user", "parts": [{"kind": "text", "text": relay_text}]}

            # rotate labels for next hop
            if next_target_label == "A":
                current_label, other_label = "A", "B"
            else:
                current_label, other_label = "B", "A"
        else:
            # no relay -> stop
            next_message = None

    return events, {"a": agent_a.card}, {"b": agent_b.card}


# ---------- Gradio UI ----------

def on_connect(a_url, a_user, a_pass, a_extra, b_url, b_user, b_pass, b_extra):
    a = AgentConfig("A", a_url, a_user, a_pass, a_extra)
    b = AgentConfig("B", b_url, b_user, b_pass, b_extra)
    a.headers = build_headers(a.username, a.password, a.extra_headers_json)
    b.headers = build_headers(b.username, b.password, b.extra_headers_json)
    a.card = fetch_agent_card(a.base_url, a.headers)
    b.card = fetch_agent_card(b.base_url, b.headers)
    a_name = a.card.get("name") or a.card.get("title") or "Agent A"
    b_name = b.card.get("name") or b.card.get("title") or "Agent B"
    return (
        json.dumps(a.card, indent=2),
        json.dumps(b.card, indent=2),
        f"Connected ✓  A = {a_name}",
        f"Connected ✓  B = {b_name}",
        json.dumps({"A": a.card, "B": b.card}, indent=2),
    )

def run_demo(a_url, a_user, a_pass, a_extra, b_url, b_user, b_pass, b_extra, initiator_choice, prompt, hop_limit):
    a = AgentConfig("A", a_url, a_user, a_pass, a_extra)
    b = AgentConfig("B", b_url, b_user, b_pass, b_extra)
    a.headers = build_headers(a.username, a.password, a.extra_headers_json)
    b.headers = build_headers(b.username, b.password, b.extra_headers_json)
    a.card = fetch_agent_card(a.base_url, a.headers)
    b.card = fetch_agent_card(b.base_url, b.headers)

    events, a_meta, b_meta = auto_relay_conversation(a, b, initiator_choice, prompt, hop_limit=int(hop_limit))

    # Simple HTML transcript
    def badge(label):
        return f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;background:{"#2563eb" if label=="A" else "#16a34a"};color:white;font-size:12px;">Agent {label}</span>'
    def kind_style(k):
        return {
            "message": "",
            "status": "opacity:0.7;font-style:italic;",
            "artifact": "opacity:0.8;",
            "error": "color:#b91c1c;font-weight:600;",
        }.get(k, "")

    rows = []
    for e in events:
        time_str = time.strftime("%H:%M:%S", time.localtime(e.t))
        safe_text = (e.text or "").replace("<", "&lt;").replace(">", "&gt;")
        role = f" ({e.role})" if e.role else ""
        rows.append(
            f'<div style="margin:8px 0;"><span style="opacity:0.65">{time_str}</span> {badge(e.origin)} '
            f'<span style="opacity:0.6;">[{e.kind}{role}]</span>'
            f'<div style="margin:4px 0 0 24px;{kind_style(e.kind)};white-space:pre-wrap;">{safe_text}</div></div>'
        )
    html = "<div style='font-family:ui-sans-serif'>" + "\n".join(rows) + "</div>"

    raw = [dict(t=e.t, origin=e.origin, kind=e.kind, role=e.role, text=e.text, raw=e.raw) for e in events]
    export = json.dumps({"meta": {"A": a_meta["a"], "B": b_meta["b"]}, "events": raw}, indent=2)
    return html, export

with gr.Blocks(title="A2A Dual-Agent Viewer (Relay-by-Default)") as demo:
    gr.Markdown("### Agent2Agent (A2A) Dual-Agent Viewer · Relay-by-Default\nProvide two A2A endpoints and credentials, then let them ping-pong. Agents can opt out per turn via `metadata.relay = \"never\"`.")

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
        initiator_choice = gr.Radio(choices=["A", "B"], value="A", label="Initiate conversation from")
        hop_limit = gr.Slider(1, 50, value=8, step=1, label="Hop limit")
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