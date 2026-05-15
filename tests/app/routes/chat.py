"""Chat routes for the Matter RAG debug app.

Endpoints
---------
GET  /chat                         — Serve the React chat UI (HTML page)
POST /api/chat                     — Send a message, get an assistant reply
GET  /api/history/{session_id}     — Retrieve message history for a session
DELETE /api/session/{session_id}   — Clear / delete a session
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from tests.app.services.mcp_chat import run_mcp_chat, supports_tool_use
from tests.app.services.pipeline_adapter import ChatPayload, run_pipeline
from tests.app.services.session_store import store as session_store

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    include_context: bool = False   # if True, RAG context is returned in the response


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    context: Optional[str] = None   # populated when include_context=True


class HistoryResponse(BaseModel):
    session_id: str
    messages: list
    system_prompt: str


# ---------------------------------------------------------------------------
# React Chat UI (inline, no build step — CDN React + Babel standalone)
# ---------------------------------------------------------------------------

_CHAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Matter RAG // Chat</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500&family=Syne:wght@700;800&display=swap" rel="stylesheet">
  <script src="https://unpkg.com/react@18/umd/react.production.min.js" crossorigin></script>
  <script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js" crossorigin></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <style>
    :root {
      --bg:    #05060a;
      --surf:  #0c0e14;
      --card:  #0f1119;
      --sb:    #0a0c12;
      --bdr:   #1b1e2a;
      --bdrh:  #2c3044;
      --text:  #cdd2e8;
      --muted: #50566e;
      --cyan:  #00e5ff;
      --cdim:  rgba(0,229,255,.09);
      --cring: rgba(0,229,255,.18);
      --org:   #ff6830;
      --grn:   #2de08a;
      --amb:   #f5a623;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html, body, #root { height: 100%; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'JetBrains Mono', monospace;
      font-size: 13px;
      overflow: hidden;
    }
    /* dot grid */
    body::before {
      content:''; position:fixed; inset:0; z-index:0; pointer-events:none;
      background-image: radial-gradient(circle,#191c27 1px,transparent 1px);
      background-size:26px 26px; opacity:.3;
    }
    /* top line */
    body::after {
      content:''; position:fixed; top:0; left:0; right:0; height:1px; z-index:200;
      background:linear-gradient(90deg,transparent,var(--cyan) 50%,transparent);
    }
    #root { position:relative; z-index:1; display:flex; flex-direction:column; }

    /* === LAYOUT === */
    .shell { display:flex; height:100vh; overflow:hidden; }

    /* --- SIDEBAR --- */
    .sidebar {
      width: 220px; flex-shrink:0;
      background: var(--sb);
      border-right: 1px solid var(--bdr);
      display:flex; flex-direction:column;
      overflow:hidden;
    }
    .sb-logo {
      padding: 14px 16px 12px;
      border-bottom: 1px solid var(--bdr);
      display:flex; align-items:center; gap:9px;
    }
    .hex {
      width:24px; height:24px; flex-shrink:0;
      clip-path:polygon(50% 0%,100% 25%,100% 75%,50% 100%,0% 75%,0% 25%);
      background:var(--cdim); border:1px solid var(--cyan);
      display:flex; align-items:center; justify-content:center;
      font-family:'Syne',sans-serif; font-size:9px; font-weight:800; color:var(--cyan);
    }
    .sb-title { font-family:'Syne',sans-serif; font-weight:800; font-size:12px;
      letter-spacing:.06em; text-transform:uppercase; color:#fff; }
    .sb-section {
      padding:10px 16px 6px;
      font-size:9px; letter-spacing:.18em; text-transform:uppercase; color:var(--muted);
    }
    .sb-item {
      display:flex; align-items:center; gap:8px;
      padding:7px 16px; font-size:11px; color:var(--muted);
      cursor:pointer; text-decoration:none; transition:color .15s,background .15s;
      border-left:2px solid transparent;
    }
    .sb-item:hover { color:var(--text); background:rgba(255,255,255,.03); }
    .sb-item.active { color:var(--cyan); border-left-color:var(--cyan); background:var(--cdim); }
    .sb-item svg { width:12px; height:12px; flex-shrink:0; }
    .sb-divider { height:1px; background:var(--bdr); margin:8px 0; }
    .sb-session {
      margin:0 12px 8px;
      border:1px solid var(--bdr);
      background:var(--card);
      padding:10px 12px;
    }
    .sb-sess-lbl { font-size:9px; letter-spacing:.15em; text-transform:uppercase;
      color:var(--muted); margin-bottom:5px; }
    .sb-sess-id { font-size:10px; color:var(--cyan); word-break:break-all; line-height:1.5; }
    .sb-new-btn {
      margin:0 12px 12px;
      background:none; border:1px solid var(--bdr);
      color:var(--muted); font-family:inherit; font-size:10px;
      padding:6px; cursor:pointer; width:calc(100% - 24px);
      letter-spacing:.05em; transition:all .2s;
    }
    .sb-new-btn:hover { border-color:var(--cyan); color:var(--cyan); }
    .sb-push { flex:1; }
    .sb-status {
      padding:12px 16px; border-top:1px solid var(--bdr);
      font-size:10px; color:var(--muted);
    }
    .status-dot {
      display:inline-block; width:6px; height:6px; border-radius:50%;
      background:var(--muted); margin-right:6px; vertical-align:middle;
      transition:background .3s;
    }
    .status-dot.ok  { background:var(--grn);  box-shadow:0 0 6px var(--grn); }
    .status-dot.err { background:var(--org);  box-shadow:0 0 6px var(--org); }

    /* --- MAIN CHAT AREA --- */
    .main { flex:1; display:flex; flex-direction:column; min-width:0; overflow:hidden; }

    /* Chat header */
    .chat-hdr {
      border-bottom:1px solid var(--bdr);
      background:rgba(5,6,10,.85); backdrop-filter:blur(10px);
      padding:0 20px; height:48px;
      display:flex; align-items:center; gap:12px; flex-shrink:0;
    }
    .chat-hdr-title { font-family:'Syne',sans-serif; font-weight:800;
      font-size:12px; letter-spacing:.08em; text-transform:uppercase; color:var(--cyan); }
    .chat-hdr-sep { color:var(--bdr); }
    .chat-hdr-sub { font-size:10px; color:var(--muted); }
    .chat-hdr-r { margin-left:auto; display:flex; align-items:center; gap:10px; }
    .hdr-badge {
      font-size:9px; padding:2px 8px; border:1px solid var(--bdr);
      color:var(--muted); letter-spacing:.06em;
    }
    .hdr-link { font-size:10px; color:var(--muted); text-decoration:none;
      letter-spacing:.04em; transition:color .2s; }
    .hdr-link:hover { color:var(--cyan); }

    /* Messages */
    .msgs {
      flex:1; overflow-y:auto; padding:24px 24px 12px;
      display:flex; flex-direction:column; gap:18px;
      scroll-behavior:smooth;
    }
    .msgs::-webkit-scrollbar { width:4px; }
    .msgs::-webkit-scrollbar-track { background:transparent; }
    .msgs::-webkit-scrollbar-thumb { background:var(--bdr); border-radius:2px; }

    .msg { display:flex; gap:10px; max-width:820px; }
    .msg.user { align-self:flex-end; flex-direction:row-reverse; }

    .avatar {
      width:28px; height:28px; flex-shrink:0;
      display:flex; align-items:center; justify-content:center;
      font-size:9px; font-weight:600; letter-spacing:.04em;
    }
    .avatar.user {
      background:rgba(0,229,255,.15);
      border:1px solid var(--cyan);
      color:var(--cyan);
    }
    .avatar.asst {
      background:rgba(255,255,255,.04);
      border:1px solid var(--bdr);
      color:var(--muted);
    }

    .bubble {
      padding:10px 14px; line-height:1.65;
      font-size:12px; max-width:680px;
      white-space:pre-wrap; word-break:break-word;
    }
    .bubble.user {
      background:var(--cdim);
      border:1px solid var(--cring);
      color:var(--text);
      border-bottom-right-radius:0;
    }
    .bubble.asst {
      background:var(--card);
      border:1px solid var(--bdr);
      color:var(--text);
      border-bottom-left-radius:0;
    }
    .bubble.err {
      background:rgba(255,104,48,.08);
      border:1px solid rgba(255,104,48,.3);
      color:var(--org);
    }
    .msg-meta { font-size:9px; color:var(--muted); margin-top:4px; letter-spacing:.04em; }
    .msg.user .msg-meta { text-align:right; }

    /* Thinking */
    .thinking { display:flex; gap:5px; align-items:center; padding:6px 0; }
    .thinking span {
      width:5px; height:5px; border-radius:50%; background:var(--cyan);
      animation:tpulse 1.3s ease-in-out infinite;
    }
    .thinking span:nth-child(2){ animation-delay:.22s; }
    .thinking span:nth-child(3){ animation-delay:.44s; }
    @keyframes tpulse {
      0%,80%,100%{ transform:translateY(0);   opacity:.35; }
      40%         { transform:translateY(-5px); opacity:1; }
    }

    /* Empty state */
    .empty {
      flex:1; display:flex; flex-direction:column;
      align-items:center; justify-content:center; gap:10px;
      padding:2rem;
    }
    .empty-hex {
      width:52px; height:52px;
      clip-path:polygon(50% 0%,100% 25%,100% 75%,50% 100%,0% 75%,0% 25%);
      background:var(--cdim); border:1px solid rgba(0,229,255,.25);
      display:flex; align-items:center; justify-content:center; margin-bottom:4px;
    }
    .empty-hex svg { width:22px; height:22px; color:var(--cyan); opacity:.7; }
    .empty h2 {
      font-family:'Syne',sans-serif; font-weight:800; font-size:1rem;
      color:#fff; letter-spacing:-.01em;
    }
    .empty p { font-size:11px; color:var(--muted); text-align:center;
      max-width:380px; line-height:1.8; }
    .empty-hints {
      display:flex; flex-wrap:wrap; gap:6px; justify-content:center; margin-top:6px;
    }
    .hint-pill {
      font-size:10px; padding:5px 12px;
      border:1px solid var(--bdr); color:var(--muted);
      cursor:pointer; letter-spacing:.03em; transition:all .2s;
    }
    .hint-pill:hover { border-color:var(--cyan); color:var(--cyan); background:var(--cdim); }

    /* Input */
    .input-wrap {
      border-top:1px solid var(--bdr);
      background:rgba(5,6,10,.9);
      padding:14px 20px 16px; flex-shrink:0;
    }
    .input-row {
      display:flex; gap:0; max-width:840px; margin:0 auto;
      border:1px solid var(--bdr); background:var(--surf);
      transition:border-color .2s;
    }
    .input-row:focus-within { border-color:var(--cyan); }
    .prompt-glyph {
      padding:0 12px; display:flex; align-items:center;
      color:var(--cyan); font-size:14px; user-select:none; flex-shrink:0;
    }
    textarea {
      flex:1; background:transparent; border:none; outline:none;
      color:var(--text); padding:10px 8px 10px 0;
      font-family:'JetBrains Mono',monospace; font-size:12px;
      resize:none; min-height:42px; max-height:140px; line-height:1.55;
    }
    textarea::placeholder { color:var(--muted); }
    .send-btn {
      background:var(--cdim); border:none; border-left:1px solid var(--bdr);
      color:var(--cyan); font-family:inherit; font-size:10px;
      padding:0 16px; cursor:pointer; letter-spacing:.08em; text-transform:uppercase;
      transition:background .2s; flex-shrink:0;
    }
    .send-btn:hover:not(:disabled) { background:rgba(0,229,255,.18); }
    .send-btn:disabled { color:var(--muted); cursor:not-allowed; background:transparent; }
    .input-hint { font-size:9px; color:var(--muted); text-align:center;
      margin-top:7px; letter-spacing:.05em; }

    .ctx-panel {
      margin-top:6px; border:1px solid var(--bdr); background:var(--sb);
      font-size:10px; color:var(--muted);
    }
    .ctx-toggle {
      width:100%; background:none; border:none; border-bottom:1px solid var(--bdr);
      color:var(--muted); font-family:inherit; font-size:9px; letter-spacing:.08em;
      text-transform:uppercase; padding:4px 10px; cursor:pointer; text-align:left;
      transition:color .15s;
    }
    .ctx-toggle:hover { color:var(--cyan); }
    .ctx-body {
      padding:8px 10px; white-space:pre-wrap; word-break:break-word;
      max-height:220px; overflow-y:auto; line-height:1.55;
    }
    .ctx-body::-webkit-scrollbar { width:3px; }
    .ctx-body::-webkit-scrollbar-thumb { background:var(--bdr); }
    .show-ctx-btn {
      background:none; border:1px solid var(--bdr); color:var(--muted);
      font-family:inherit; font-size:9px; letter-spacing:.08em; padding:3px 10px;
      cursor:pointer; margin-left:auto; transition:all .2s; white-space:nowrap;
    }
    .show-ctx-btn.on  { border-color:var(--cyan); color:var(--cyan); }
    .show-ctx-btn:hover { border-color:var(--cyan); color:var(--cyan); }

    /* Message enter animation */
    @keyframes msgIn { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }
    .msg-enter { animation:msgIn .25s ease forwards; }
  </style>
</head>
<body>
<div id="root"></div>

<script type="text/babel">
const { useState, useEffect, useRef, useCallback } = React;

const HINTS = [
  "Which test cases cover On/Off cluster?",
  "Is there a TC for device discovery?",
  "What does TC-OO-2.1 test?",
  "Show gaps in Commissioning test coverage",
];

function useSession() {
  const [sid, setSid] = useState(() => sessionStorage.getItem("mrq_sid") || "");
  const set = useCallback(id => { sessionStorage.setItem("mrq_sid", id); setSid(id); }, []);
  const clear = useCallback(() => { sessionStorage.removeItem("mrq_sid"); setSid(""); }, []);
  return [sid, set, clear];
}

function Avatar({ role }) {
  return <div className={`avatar ${role}`}>{role === "user" ? "YOU" : "RAG"}</div>;
}

function ContextPanel({ ctx }) {
  const [open, setOpen] = React.useState(false);
  if (!ctx) return null;
  return (
    <div className="ctx-panel">
      <button className="ctx-toggle" onClick={() => setOpen(o => !o)}>
        {open ? "\u25b2 hide" : "\u25bc show"} retrieved context ({ctx.split("\\n\\n").length} snippets)
      </button>
      {open && <div className="ctx-body">{ctx}</div>}
    </div>
  );
}

function Msg({ msg }) {
  const ts = new Date(msg.ts).toTimeString().slice(0,8);
  const roleMap = { user:"user", assistant:"asst", error:"err" };
  const cls = roleMap[msg.role] || "asst";
  return (
    <div className={`msg msg-enter ${msg.role === "user" ? "user" : "assistant"}`}>
      <Avatar role={msg.role === "user" ? "user" : "asst"} />
      <div style={{minWidth:0}}>
        <div className={`bubble ${cls}`}>{msg.content}</div>
        {msg.context && <ContextPanel ctx={msg.context}/>}
        <div className="msg-meta">{ts}</div>
      </div>
    </div>
  );
}

function Thinking() {
  return (
    <div className="msg assistant msg-enter">
      <Avatar role="asst" />
      <div className="bubble asst">
        <div className="thinking"><span/><span/><span/></div>
      </div>
    </div>
  );
}

function Sidebar({ sid, onNew, health }) {
  const shortSid = sid ? sid.slice(0,8) + "…" : "—";
  return (
    <aside className="sidebar">
      <div className="sb-logo">
        <div className="hex">M</div>
        <span className="sb-title">Matter RAG</span>
      </div>

      <div className="sb-section">Navigation</div>
      <a href="/" className="sb-item">
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" d="M3 12l9-9 9 9M5 10v9h14v-9"/>
        </svg>
        Dashboard
      </a>
      <a href="/chat" className="sb-item active">
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-3 3v-3z"/>
        </svg>
        Chat
      </a>
      <a href="/test-cases" className="sb-item">
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" d="M9 12l2 2 4-4M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2"/>
        </svg>
        Test Cases
      </a>
      <a href="/kg/nodes" className="sb-item">
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <circle cx="12" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/>
          <path stroke-linecap="round" d="M12 7v4M12 11l-5 6M12 11l5 6"/>
        </svg>
        Knowledge Graph
      </a>
      <a href="/docs" target="_blank" className="sb-item">
        <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" d="M9 12h6m-3-3v6M3 12a9 9 0 1018 0A9 9 0 003 12z"/>
        </svg>
        API Docs ↗
      </a>

      <div className="sb-divider"/>

      <div className="sb-section">Session</div>
      <div className="sb-session">
        <div className="sb-sess-lbl">session id</div>
        <div className="sb-sess-id">{sid || "no session yet"}</div>
      </div>
      <button className="sb-new-btn" onClick={onNew}>+ NEW SESSION</button>

      <div className="sb-push"/>
      <div className="sb-status">
        <span className={`status-dot ${health}`}/>
        pipeline {health === "ok" ? "operational" : health === "err" ? "error" : "checking"}
      </div>
    </aside>
  );
}

function ChatApp() {
  const [sid, setSid, clearSid] = useSession();
  const [msgs, setMsgs] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [health, setHealth] = useState("init");
  const [showCtx, setShowCtx] = useState(false);
  const endRef = useRef(null);
  const taRef  = useRef(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior:"smooth" }); }, [msgs, loading]);

  // Probe health
  useEffect(() => {
    fetch('/health').then(r=>r.json()).then(d=>{
      setHealth(d.status === 'ok' ? 'ok' : 'err');
    }).catch(()=>setHealth('err'));
  }, []);

  // Restore history
  useEffect(() => {
    if (!sid) return;
    fetch(`/api/history/${sid}`).then(r=>r.ok?r.json():null).then(data=>{
      if (!data) return;
      setMsgs(data.messages.map((m,i)=>({
        id:`h${i}`, role:m.role, content:m.content, ts:Date.now()-i*1000
      })));
    }).catch(()=>{});
  }, []);

  const send = useCallback(async (text) => {
    const t = (text || input).trim();
    if (!t || loading) return;
    setMsgs(p=>[...p, {id:`u${Date.now()}`, role:"user", content:t, ts:Date.now()}]);
    setInput("");
    setLoading(true);
    try {
      const res = await fetch("/api/chat", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({session_id:sid||null, message:t, include_context:showCtx}),
      });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail||"Request failed");
      if (d.session_id && d.session_id !== sid) setSid(d.session_id);
      setMsgs(p=>[...p, {id:`a${Date.now()}`, role:"assistant", content:d.reply, context:d.context||null, ts:Date.now()}]);
    } catch(e) {
      setMsgs(p=>[...p, {id:`e${Date.now()}`, role:"error", content:`Error: ${e.message}`, ts:Date.now()}]);
    } finally {
      setLoading(false);
      taRef.current?.focus();
    }
  }, [input, loading, sid, setSid]);

  const onKey = e => { if (e.key==="Enter" && !e.shiftKey){ e.preventDefault(); send(); } };
  const onNew = () => { clearSid(); setMsgs([]); setInput(""); taRef.current?.focus(); };

  return (
    <div className="shell">
      <Sidebar sid={sid} onNew={onNew} health={health}/>
      <div className="main">
        <div className="chat-hdr">
          <span className="chat-hdr-title">Chat</span>
          <span className="chat-hdr-sep">|</span>
          <span className="chat-hdr-sub">Matter Expert · FAISS + KG grounded</span>
          <div className="chat-hdr-r">
            <button
              className={`show-ctx-btn ${showCtx ? "on" : ""}`}
              onClick={() => setShowCtx(v => !v)}
              title="When on, retrieved RAG context is shown below each assistant reply"
            >
              {showCtx ? "CTX ON" : "CTX OFF"}
            </button>
            <span className="hdr-badge">{sid ? sid.slice(0,8)+"…" : "new session"}</span>
            <a href="/" className="hdr-link">← Dashboard</a>
          </div>
        </div>

        <div className="msgs">
          {msgs.length === 0 && !loading && (
            <div className="empty">
              <div className="empty-hex">
                <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
                  <path stroke-linecap="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/>
                </svg>
              </div>
              <h2>Matter RAG Assistant</h2>
              <p>Ask about test case coverage, PR changes, or Matter spec features.
                 Answers are grounded in the FAISS vector DB and knowledge graph.</p>
              <div className="empty-hints">
                {HINTS.map((h,i) => (
                  <div key={i} className="hint-pill" onClick={() => send(h)}>{h}</div>
                ))}
              </div>
            </div>
          )}
          {msgs.map(m => <Msg key={m.id} msg={m}/>)}
          {loading && <Thinking/>}
          <div ref={endRef}/>
        </div>

        <div className="input-wrap">
          <div className="input-row">
            <span className="prompt-glyph">&gt;&gt;</span>
            <textarea
              ref={taRef}
              value={input}
              onChange={e=>setInput(e.target.value)}
              onKeyDown={onKey}
              placeholder="Ask about test cases, PR coverage, Matter spec clusters…"
              rows={1}
              autoFocus
            />
            <button className="send-btn" onClick={()=>send()} disabled={!input.trim()||loading}>
              {loading ? "…" : "SEND"}
            </button>
          </div>
          <div className="input-hint">Enter to send · Shift+Enter for newline</div>
        </div>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<ChatApp/>);
</script>
</body>
</html>
"""


_embedder_load_lock = threading.Lock()
_embedder_loading = False


def _warm_embedder_background() -> None:
    """Load the embedding model in a background thread (called on first GET /chat)."""
    global _embedder_loading
    from tests.app.main import _get_embedder
    with _embedder_load_lock:
        if _embedder_loading:
            return
        _embedder_loading = True
    try:
        _get_embedder()
        logger.info("[chat_ui] Embeddings model warm-up complete")
    except Exception as exc:
        logger.warning("[chat_ui] Embeddings warm-up failed: %s", exc)


# ---------------------------------------------------------------------------
# GET /chat  — serve React UI
# ---------------------------------------------------------------------------

@router.get("/chat", response_class=HTMLResponse, include_in_schema=False)
async def chat_ui():
    """Serve the Matter RAG chat interface.

    Also kicks off a background thread to warm up the embedding model so it is
    ready before the user sends their first message.
    """
    from tests.app.main import _state
    if _state.embedder is None:
        t = threading.Thread(target=_warm_embedder_background, daemon=True)
        t.start()
    return HTMLResponse(content=_CHAT_HTML)


# ---------------------------------------------------------------------------
# POST /api/chat
# ---------------------------------------------------------------------------

@router.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Send a user message and receive an assistant reply.

    Creates a new session automatically when ``session_id`` is omitted.

    When ``provider: claude_cli`` is configured the endpoint uses the
    agentic tool-use path (``mcp_chat``) — the LLM decides which KG / FAISS
    tools to call.  Other providers fall back to the original two-step
    pipeline (query-planner → KG dispatch → LLM response).

    Example body::

        {"session_id": null, "message": "Which test cases cover On/Off cluster?"}
    """
    from tests.app.main import _state, _get_embedder  # late import avoids circular deps
    from src.engine.run_context import create_run_context, set_run_context
    import copy
    from src.llm.llm_provider import get_llm

    # Ensure embedder is ready — normally pre-warmed by GET /chat, but guard here too
    _get_embedder()

    # Get or create session
    session = session_store.get_or_create(req.session_id)
    sid = session.session_id

    # Store user turn
    session_store.append_message(sid, "user", req.message)

    # Build history (exclude the turn we just appended)
    history = session_store.get_messages(sid)
    history_without_current = history[:-1]

    # Create per-request run context
    run_ctx = create_run_context("app_chat")
    token = set_run_context(run_ctx)
    logger.info("[chat] session=%s  run_dir=%s", sid, run_ctx.run_dir)

    try:
        # ── Try agentic MCP tool-use path ─────────────────────────────────────
        # Use it when the provider supports tool_use (claude_cli / ClaudeProvider).
        # Fall back to the two-step graph path for all other providers.
        _use_mcp = False
        if _state.config is not None:
            try:
                _probe_llm = get_llm(copy.copy(_state.config.llm))
                _use_mcp = supports_tool_use(_probe_llm)
            except Exception as _probe_err:
                logger.debug("[chat] tool-use probe failed: %s", _probe_err)

        if _use_mcp:
            logger.info("[chat] using agentic MCP tool-use path for session=%s", sid)
            reply, rag_ctx = await asyncio.wait_for(
                run_mcp_chat(
                    user_message=req.message,
                    system_prompt=session_store.get_system_prompt(sid),
                    chat_history=history_without_current,
                    app_state=_state,
                    run_ctx=run_ctx,
                ),
                timeout=90,
            )
        else:
            logger.info("[chat] using classic two-step pipeline for session=%s", sid)
            payload = ChatPayload(
                session_id=sid,
                user_message=req.message,
                system_prompt=session_store.get_system_prompt(sid),
                chat_history=history_without_current,
                metadata={},
            )
            reply, rag_ctx = await asyncio.wait_for(
                run_pipeline(payload, _state, run_ctx=run_ctx),
                timeout=90,
            )

    except asyncio.TimeoutError:
        logger.warning("[chat] request timed out after 90s for session=%s", sid)
        reply = "The request timed out (90s limit). The LLM took too long to respond — please try again or simplify your question."
        rag_ctx = ""

    finally:
        run_ctx.close()
        from src.engine.run_context import _current_run_ctx
        _current_run_ctx.reset(token)

    # Store assistant turn
    session_store.append_message(sid, "assistant", reply)

    return ChatResponse(
        session_id=sid,
        reply=reply,
        context=rag_ctx if req.include_context else None,
    )


# ---------------------------------------------------------------------------
# GET /api/history/{session_id}
# ---------------------------------------------------------------------------

@router.get("/api/history/{session_id}", response_model=HistoryResponse)
async def get_history(session_id: str):
    """Return the full message history for a session."""
    session = session_store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    return HistoryResponse(
        session_id=session_id,
        messages=session_store.get_messages(session_id),
        system_prompt=session_store.get_system_prompt(session_id),
    )


# ---------------------------------------------------------------------------
# DELETE /api/session/{session_id}
# ---------------------------------------------------------------------------

@router.delete("/api/session/{session_id}")
async def delete_session(session_id: str):
    """Delete a chat session and all its history."""
    deleted = session_store.delete(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return {"status": "deleted", "session_id": session_id}
