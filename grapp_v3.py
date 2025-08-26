"""
A2A Dual-Agent Viewer (Gradio) – Split UI (Chat + Relay)

Left:   Chat interface (persisted prompt→reply pairs) with initiator A|B
Right:  Relay timeline (colored blocks by agent) filtered to the selected request

Policy: Relay-by-default. Agents can opt-out per-turn via
  message.metadata.relay in {"never","no",false} or message.metadata.doNotRelay == true
Optional targeting via message.metadata.delegateTo in {"A","B"}

Requires: pip install gradio requests

Notes:
- Works with two running A2A servers that expose /v1/card and /v1/message:stream
- Basic-Auth supported; optional extra headers JSON
- Chat selection updates the relay timeline; also a fallback Request picker is provided
"""

from __future__ import annotations
import base64
import json
import time
import uuid
from typing import Any, Dict, Generator, List, Optional, Tuple

import gradio as gr
import requests

# ------------------------------- A2A helpers ---------------------------------

TERMINAL_STATES = {"completed", "canceled", "rejected", "failed"}
RELAY_BY_DEFAULT = True


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
        buf: List[str] = []
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
    parts = (message.get("parts") or [])
    acc: List[str] = []
    for p in parts:
        kind = p.get("kind")
        if kind == "text":
            acc.append(p.get("text", ""))
        elif kind in ("file", "data"):
            acc.append(f"[{kind}]")
    return "\n".join([s for s in acc if s])


# ------------------------------- Parsing --------------------------------------

def parse_stream_result(result_obj: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    result = result_obj.get("result", {}) or {}
    if not result:
        return ("unknown", result_obj)

    # direct shapes
    if "role" in result and "parts" in result:
        return ("message", result)
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


# --------------------------- Relay-by-default core ----------------------------

def send_stream_message(base_url: str, headers: Dict[str, str], message: Dict[str, Any], history_len: int = 6):
    url = base_url.rstrip("/") + "/v1/message:stream"
    payload = {
        "message": {**message, "messageId": message.get("messageId") or str(uuid.uuid4())},
        "configuration": {"historyLength": history_len},
    }
    yield from sse_stream(url, payload, headers)


def auto_relay_conversation(
    a_conf: Dict[str, Any],
    b_conf: Dict[str, Any],
    initiator_label: str,  # "A" | "B"
    user_prompt: str,
    hop_limit: int = 6,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Returns (events, first_reply_text_from_initiator)
    events: list of dicts {t, origin, kind, role, text, raw, relayed: bool}
    """
    agents = {
        "A": {"label": "A", **a_conf},
        "B": {"label": "B", **b_conf},
    }

    current = initiator_label
    other = "B" if current == "A" else "A"
    next_message = {"role": "user", "parts": [{"kind": "text", "text": user_prompt}]}

    events: List[Dict[str, Any]] = []

    def record(origin: str, kind: str, role: Optional[str], text: str, raw: Dict[str, Any], relayed: bool = False):
        events.append({
            "t": time.time(),
            "origin": origin,
            "kind": kind,
            "role": role,
            "text": text,
            "raw": raw,
            "relayed": relayed,
        })

    # Capture the *first* reply from the initiator to show in Chatbot
    first_initiator_reply: Optional[str] = None

    hops = 0
    while hops < hop_limit and next_message is not None:
        sender = agents[current]
        receiver = agents[other]

        relay_text: Optional[str] = None
        relay_allowed: bool = RELAY_BY_DEFAULT
        explicit_target: Optional[str] = None
        stream_reached_terminal = False
        last_msg_event_idx: Optional[int] = None

        try:
            for frame in send_stream_message(sender["base_url"], sender["headers"], next_message):
                typ, payload = parse_stream_result(frame)

                if typ == "message":
                    role = payload.get("role")
                    text = extract_text_parts(payload).strip()
                    record(sender["label"], "message", role, text or "[non-text parts]", frame)
                    last_msg_event_idx = len(events) - 1

                    # First reply from the initiator (to show in Chatbot)
                    if first_initiator_reply is None and sender["label"] == initiator_label and text:
                        first_initiator_reply = text

                    meta = (payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
                    if str(meta.get("relay")).lower() in {"never", "no", "false"} or bool(meta.get("doNotRelay")):
                        relay_allowed = False
                    if meta.get("delegateTo") in {"A", "B"}:
                        explicit_target = meta["delegateTo"]

                    if text:
                        relay_text = text

                elif typ == "task":
                    st = (payload.get("status") or {}).get("state", "")
                    record(sender["label"], "status", None, f"task {payload.get('id')} -> {st}", frame)

                elif typ == "statusUpdate":
                    status = (payload.get("status") or {}).get("state", "")
                    is_final = bool(payload.get("final"))
                    msg_txt = (payload.get("status") or {}).get("message", "")
                    record(sender["label"], "status", None, f"{status}{(' - ' + msg_txt) if msg_txt else ''}", frame)
                    if is_final or status in TERMINAL_STATES:
                        stream_reached_terminal = True

                elif typ == "artifactUpdate":
                    record(sender["label"], "artifact", None, f"artifact update for task {payload.get('taskId')}", frame)

        except Exception as e:
            record("system", "error", None, f"{sender['label']} stream error: {e}", {"exception": str(e)})
            relay_text = None
            relay_allowed = False
            stream_reached_terminal = True

        hops += 1

        # Mark the *relayed* message from this hop (if any)
        if relay_allowed and relay_text and last_msg_event_idx is not None:
            events[last_msg_event_idx]["relayed"] = True
            # decide the next target
            if explicit_target in {"A", "B"}:
                next_target = explicit_target
            else:
                next_target = other
            next_message = {"role": "user", "parts": [{"kind": "text", "text": relay_text}]}
            current, other = next_target, ("B" if next_target == "A" else "A")
        else:
            next_message = None

    return events, (first_initiator_reply or "(no immediate reply)")


# ------------------------------- UI helpers -----------------------------------

def render_card_json(card: Dict[str, Any]) -> str:
    return json.dumps(card or {}, indent=2)


def render_relay_html(events: List[Dict[str, Any]]) -> str:
    """Only show relayed agent messages, color-coded by origin (A = blue, B = green)."""
    blocks: List[str] = []
    legend = (
        "<div style='margin-bottom:8px;opacity:0.7'>"
        "<span style='display:inline-block;width:10px;height:10px;background:#2563eb;margin-right:6px;border-radius:2px'></span>A "
        "<span style='display:inline-block;width:10px;height:10px;background:#16a34a;margin:0 6px;border-radius:2px'></span>B"
        "</div>"
    )
    for ev in events:
        if ev.get("kind") != "message" or not ev.get("relayed"):
            continue
        origin = ev.get("origin")
        bg = "#e2e8f0"
        border = "#cbd5e1"
        if origin == "A":
            bg, border = "#dbeafe", "#93c5fd"  # blue-ish
        elif origin == "B":
            bg, border = "#dcfce7", "#86efac"  # green-ish
        t = time.strftime("%H:%M:%S", time.localtime(ev.get("t", time.time())))
        text = (ev.get("text") or "").replace("<", "&lt;").replace(">", "&gt;")
        blocks.append(
            f"<div style='background:{bg};border:1px solid {border};border-radius:10px;padding:8px 12px;margin:8px 0'>"
            f"<div style='font-size:12px;opacity:0.7'>{t} · Agent <b>{origin}</b> (relayed)</div>"
            f"<div style='white-space:pre-wrap;margin-top:4px'>{text}</div>"
            f"</div>"
        )
    if not blocks:
        blocks = ["<div style='opacity:0.6'>No relayed messages for this request yet.</div>"]
    return "<div style='font-family:ui-sans-serif'>" + legend + "\n".join(blocks) + "</div>"


def build_request_label(idx: int, initiator: str, prompt: str) -> str:
    p = (prompt or "").strip().replace("\n", " ")
    if len(p) > 80:
        p = p[:77] + "…"
    return f"#{idx+1} [{initiator}] {p}"


# ------------------------------- Gradio App -----------------------------------

def app():
    with gr.Blocks(title="A2A Dual-Agent Viewer – Split UI (Chat + Relay)") as demo:
        gr.Markdown("""
        ### A2A Dual-Agent Viewer · Split UI (Chat + Relay)
        *Left:* chat with the initiator agent. *Right:* shows **relayed** messages between A ↔ B
        """)

        # ---- Connection setup
        with gr.Row():
            with gr.Column():
                gr.Markdown("**Agent A**")
                a_url = gr.Textbox(label="Base URL (e.g., http://localhost:5555)")
                a_user = gr.Textbox(label="Basic-Auth Username")
                a_pass = gr.Textbox(label="Basic-Auth Password", type="password")
                a_extra = gr.Textbox(label="Extra headers (JSON)", placeholder='{"X-API-Key":"..."}')
                a_card_code = gr.Code(label="Agent A Card (GET /v1/card)", interactive=False)
            with gr.Column():
                gr.Markdown("**Agent B**")
                b_url = gr.Textbox(label="Base URL (e.g., http://localhost:6666)")
                b_user = gr.Textbox(label="Basic-Auth Username")
                b_pass = gr.Textbox(label="Basic-Auth Password", type="password")
                b_extra = gr.Textbox(label="Extra headers (JSON)", placeholder='{"X-API-Key":"..."}')
                b_card_code = gr.Code(label="Agent B Card (GET /v1/card)", interactive=False)

        connect_btn = gr.Button("Connect & Fetch Cards")
        connect_status = gr.Markdown(visible=False)

        # Internal state
        a_conf_state = gr.State({})  # {base_url, headers, card}
        b_conf_state = gr.State({})
        sessions_state = gr.State([])  # list of dicts: {id, prompt, initiator, events, reply}

        gr.Markdown("---")

        # ---- Split UI: Left (Chat) | Right (Relay)
        with gr.Row():
            # LEFT PANEL
            with gr.Column(scale=1):
                with gr.Row():
                    initiator_choice = gr.Radio(choices=["A", "B"], value="A", label="Initiate conversation from")
                    hop_limit = gr.Slider(1, 50, value=8, step=1, label="Hop limit")
                user_input = gr.Textbox(label="Your prompt", lines=2, placeholder="Ask Agent A or B…")
                send_btn = gr.Button("Send & Relay")
                chat = gr.Chatbot(label="Your Requests (one pair per request)", height=360)
                # Fallback request picker (also kept in sync)
                req_picker = gr.Dropdown(label="Requests", choices=[], value=None)

            # RIGHT PANEL
            with gr.Column(scale=1):
                relay_title = gr.Markdown("**Relayed Messages for Selected Request**")
                relay_html = gr.HTML()

        # ----------------- Handlers -----------------
        def on_connect(a_url_v, a_user_v, a_pass_v, a_extra_v, b_url_v, b_user_v, b_pass_v, b_extra_v):
            try:
                a_headers = build_headers(a_user_v or "", a_pass_v or "", a_extra_v or "")
                b_headers = build_headers(b_user_v or "", b_pass_v or "", b_extra_v or "")
                a_card = fetch_agent_card(a_url_v, a_headers)
                b_card = fetch_agent_card(b_url_v, b_headers)
                a_conf = {"base_url": a_url_v, "headers": a_headers, "card": a_card}
                b_conf = {"base_url": b_url_v, "headers": b_headers, "card": b_card}
                status = "Connected ✓"
                return (
                    render_card_json(a_card),
                    render_card_json(b_card),
                    gr.update(visible=True, value=status),
                    a_conf,
                    b_conf,
                )
            except Exception as e:
                return ("", "", gr.update(visible=True, value=f"Connection failed: {e}"), {}, {})

        connect_btn.click(
            fn=on_connect,
            inputs=[a_url, a_user, a_pass, a_extra, b_url, b_user, b_pass, b_extra],
            outputs=[a_card_code, b_card_code, connect_status, a_conf_state, b_conf_state],
        )

        def list_request_options(sessions: List[Dict[str, Any]]):
            choices = [build_request_label(i, s["initiator"], s["prompt"]) for i, s in enumerate(sessions)]
            values = [s["id"] for s in sessions]
            return choices, values

        def on_send(prompt_v: str, initiator_v: str, hop_limit_v: int, a_conf: Dict[str, Any], b_conf: Dict[str, Any],
                    sessions: List[Dict[str, Any]], chat_history: List[Tuple[str, str]]):
            if not prompt_v.strip():
                return (gr.update(), gr.update(), sessions, chat_history, gr.update(), gr.update())
            if not a_conf or not b_conf:
                raise gr.Error("Connect to both agents first.")

            # Run conversation
            events, first_reply = auto_relay_conversation(a_conf, b_conf, initiator_v, prompt_v, hop_limit=int(hop_limit_v))

            # Create a new session entry
            new_id = str(uuid.uuid4())
            sess = {
                "id": new_id,
                "prompt": prompt_v,
                "initiator": initiator_v,
                "events": events,
                "reply": first_reply,
            }
            sessions = (sessions or []) + [sess]

            # Update chat history (one pair per request)
            chat_history = (chat_history or []) + [(prompt_v, first_reply)]

            # Update relay panel to this new request
            relay_html_out = render_relay_html(events)

            # Update picker choices
            choices, values = list_request_options(sessions)
            return (
                chat_history,
                relay_html_out,
                sessions,
                chat_history,
                gr.update(choices=values, value=new_id, label="Requests (select to view)", interactive=True),
                gr.update(value=build_request_label(len(sessions)-1, initiator_v, prompt_v)),
            )

        send_btn.click(
            fn=on_send,
            inputs=[user_input, initiator_choice, hop_limit, a_conf_state, b_conf_state, sessions_state, chat],
            outputs=[chat, relay_html, sessions_state, chat, req_picker, relay_title],
        )

        # Selecting a prior request by dropdown
        def on_pick(req_id: Optional[str], sessions: List[Dict[str, Any]]):
            if not req_id:
                return gr.update()
            for s in sessions or []:
                if s["id"] == req_id:
                    return render_relay_html(s["events"])
            return gr.update()

        req_picker.change(fn=on_pick, inputs=[req_picker, sessions_state], outputs=[relay_html])

        # Clicking in the Chatbot to focus a request (uses Gradio's select event)
        def on_chat_select(evt: gr.SelectData, sessions: List[Dict[str, Any]]):
            # evt.index is (row, col) for Chatbot; row maps to the request index
            try:
                row = evt.index[0] if isinstance(evt.index, (tuple, list)) else int(evt.index)
            except Exception:
                return gr.update(), gr.update()
            if row is None:
                return gr.update(), gr.update()
            if not sessions or row < 0 or row >= len(sessions):
                return gr.update(), gr.update()
            sess = sessions[row]
            return gr.update(value=sess["id"]), render_relay_html(sess["events"])

        chat.select(fn=on_chat_select, inputs=[sessions_state], outputs=[req_picker, relay_html])

    return demo


if __name__ == "__main__":
    app().launch()
