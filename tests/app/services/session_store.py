"""In-memory chat session store.

Each session carries:
- ``system_prompt``  — kept SEPARATE from the messages list (not counted as a turn)
- ``messages``       — list of {"role": "user"|"assistant", "content": str} dicts
- ``metadata``       — arbitrary dict for future use (model, flags, etc.)
"""
from __future__ import annotations

import threading
import uuid
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Matter expert system prompt
# ---------------------------------------------------------------------------

MATTER_SYSTEM_PROMPT = """You are a Matter protocol expert assistant embedded in a RAG-powered
test-plan analysis pipeline.

Your primary responsibilities:
1. Answer questions about the Matter specification (clusters, device types, interactions,
   security, commissioning, etc.) accurately and concisely.
2. Help engineers understand which test cases cover a given feature or PR change.
3. Interpret search results from the FAISS vector database and knowledge graph that are
   provided as context in the user's message.
4. Flag gaps — features or behaviours that appear in a PR but are NOT covered by
   existing test cases.
5. Suggest new test case titles and purposes when gaps are identified.

When RAG context is supplied, always ground your answer in that context first.

If the retrieved context does not contain enough information to answer the question,
you MUST begin your response with:

  "I could not find sufficient information about this in the current knowledge base
   (vector DB / knowledge graph). Based on my Matter specification knowledge:"

Then continue with your best answer from training knowledge. This makes it clear to the
user that the answer is not grounded in the indexed documents.

If the RAG context label says "No relevant results were found", treat it the same way —
use the disclosure prefix above and answer from Matter specification knowledge.

IMPORTANT constraints:
- Do NOT suggest Python code, shell commands, or scripts unless the user explicitly
  asks for code (e.g. "write a script", "show me Python code", "give me a CLI command").
- You are connected to a live knowledge base — relevant context has already been
  retrieved and injected above. Do not tell the user to "query the pipeline themselves".
- Always use the disclosure prefix when answering from training knowledge rather than
  the retrieved context.

Format:
- Use concise, plain language.
- For test case lists, ALWAYS present every TC in a Markdown table with columns TC-ID, Cluster, and Title.
  One row per TC — never collapse multiple TCs into prose like "the remaining N test cases...".
  If categorisation is useful (e.g. primary vs. setup step), add a "Role" column to the same table.
- For gap analysis, separate "covered" from "not covered" sections.
- Avoid reproducing large raw spec text verbatim; summarise instead.
"""


# ---------------------------------------------------------------------------
# Session data model
# ---------------------------------------------------------------------------

class Session:
    __slots__ = ("session_id", "system_prompt", "messages", "metadata")

    def __init__(
        self,
        session_id: str,
        system_prompt: str = MATTER_SYSTEM_PROMPT,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.messages: List[Dict[str, str]] = []
        self.metadata: Dict[str, Any] = metadata or {}


# ---------------------------------------------------------------------------
# Thread-safe store
# ---------------------------------------------------------------------------

class SessionStore:
    def __init__(self) -> None:
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: Optional[str] = None) -> Session:
        """Return an existing session or create a new one."""
        sid = session_id or str(uuid.uuid4())
        with self._lock:
            if sid not in self._sessions:
                self._sessions[sid] = Session(session_id=sid)
            return self._sessions[sid]

    def get(self, session_id: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(session_id)

    def append_message(self, session_id: str, role: str, content: str) -> None:
        """Append a turn to an existing session (creates session if missing)."""
        session = self.get_or_create(session_id)
        with self._lock:
            session.messages.append({"role": role, "content": content})

    def get_messages(self, session_id: str) -> List[Dict[str, str]]:
        with self._lock:
            session = self._sessions.get(session_id)
            return list(session.messages) if session else []

    def get_system_prompt(self, session_id: str) -> str:
        with self._lock:
            session = self._sessions.get(session_id)
            return session.system_prompt if session else MATTER_SYSTEM_PROMPT

    def clear(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].messages = []
                return True
            return False

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return bool(self._sessions.pop(session_id, None))

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)


# ---------------------------------------------------------------------------
# Module-level singleton (shared across all routers)
# ---------------------------------------------------------------------------

store = SessionStore()
