"""LLM call logger dispatcher.

Single module that handles all LLM call logging.  ``LoggingLLMProvider``
calls ``LLMCallLogger.log()`` once per completed call; this module writes to
three output files derived from the base ``call_log_path``:

    logs/llm_calls.jsonl   ← one JSON object per line (machine-readable, backward compat)
    logs/llm_calls.txt     ← human-readable text with call delimiters and request/response blocks
    logs/llm_calls.html    ← dark-terminal HTML with collapsible call panes (live auto-refresh)

The HTML file is fully rewritten after each call so opening it in a browser
during a pipeline run shows live progress (the page has a 5-second meta-refresh).
"""

from __future__ import annotations

import html as _html_mod
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton — set when LLMCallLogger is instantiated so that
# parse-error events can be logged without threading the logger through callers.
# ---------------------------------------------------------------------------

_active_logger: Optional["LLMCallLogger"] = None


def log_parse_error(context: str, response_preview: str) -> None:
    """Log a JSON parse failure to the active LLM call log HTML.

    Call this from ``_parse_llm_json`` when JSON parsing fails so the orange
    PARSE ERROR block appears in ``llm_calls.html`` alongside the LLM calls.
    No-op if no ``LLMCallLogger`` has been created yet this process.
    """
    if _active_logger is not None:
        _active_logger.log_event("parse_error", context, response_preview)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIVIDER = "=" * 80
_CALL_BANNER = "*" * 8 + " LLM Call #{call_id} " + "*" * 8


# ---------------------------------------------------------------------------
# HTML template helpers
# ---------------------------------------------------------------------------

_HTML_CSS = """\
<style>
  :root {
    --bg: #0d0d0d;
    --bg2: #141414;
    --bg3: #1a1a1a;
    --border: #2a2a2a;
    --green: #4caf50;
    --cyan: #00bcd4;
    --red: #ef5350;
    --amber: #ffb74d;
    --orange: #ff7043;
    --text: #e0e0e0;
    --dim: #757575;
    --pre-bg: #0a0a0a;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: "Menlo","Consolas","Monaco",monospace; font-size: 13px; }
  header { background: var(--bg2); border-bottom: 1px solid var(--border); padding: 12px 20px 8px; display: flex; flex-wrap: wrap; align-items: center; gap: 10px; position: sticky; top: 0; z-index: 10; }
  header h1 { color: var(--cyan); font-size: 15px; letter-spacing: 1px; }
  .stats { color: var(--dim); font-size: 12px; flex: 1; }
  .live-badge { background: var(--green); color: #000; border-radius: 3px; padding: 2px 8px; font-size: 11px; font-weight: bold; }
  .legend { width: 100%; padding: 6px 0 2px; display: flex; flex-wrap: wrap; gap: 14px; align-items: center; border-top: 1px solid var(--border); margin-top: 4px; }
  .legend-title { color: var(--dim); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; }
  .legend-item { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--dim); }
  main { padding: 16px 20px; max-width: 1400px; margin: 0 auto; }
  .call-block { border: 1px solid var(--border); border-radius: 4px; margin-bottom: 12px; overflow: hidden; }
  .event-block { border: 1px solid var(--orange); border-left: 3px solid var(--orange); border-radius: 4px; margin-bottom: 12px; overflow: hidden; background: #1a0800; }
  details > summary { list-style: none; cursor: pointer; padding: 10px 14px; background: var(--bg2); display: flex; align-items: center; gap: 10px; user-select: none; }
  details > summary::-webkit-details-marker { display: none; }
  details > summary::before { content: "▶"; color: var(--dim); font-size: 10px; transition: transform 0.15s; }
  details[open] > summary::before { transform: rotate(90deg); }
  .event-block details > summary { background: #1a0800; }
  .call-num { color: var(--cyan); font-weight: bold; min-width: 80px; }
  .call-provider { color: var(--amber); }
  .call-label { color: var(--green); font-style: italic; margin-left: 6px; }
  .call-duration { color: var(--dim); }
  .call-ts { color: var(--dim); font-size: 11px; margin-left: auto; }
  .badge { border-radius: 3px; padding: 1px 8px; font-size: 11px; font-weight: bold; }
  .badge-ok { background: #1b5e20; color: var(--green); }
  .badge-fail { background: #b71c1c; color: var(--red); }
  .badge-parse-error { background: #4a1500; color: var(--orange); }
  .event-context { color: var(--orange); font-weight: bold; }
  .call-body { padding: 12px 14px; background: var(--bg); display: flex; flex-direction: column; gap: 8px; }
  .section-title { color: var(--cyan); font-size: 11px; letter-spacing: 1px; margin-bottom: 4px; }
  .section-arrow-in  { color: var(--green); }
  .section-arrow-out { color: var(--amber); }
  pre { background: var(--pre-bg); border: 1px solid var(--border); border-radius: 3px; padding: 10px; white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.5; max-height: 500px; overflow-y: auto; }
  pre.system { border-left: 3px solid var(--cyan); }
  pre.request { border-left: 3px solid var(--green); }
  pre.response { border-left: 3px solid var(--amber); }
  pre.error { border-left: 3px solid var(--red); color: var(--red); }
  pre.parse-error-raw { border-left: 3px solid var(--orange); color: var(--amber); }
  .meta-row { color: var(--dim); font-size: 11px; display: flex; gap: 16px; }
  footer { text-align: center; color: var(--dim); font-size: 11px; padding: 20px; border-top: 1px solid var(--border); }
  /* ── pass filter bar ── */
  .filter-bar { width: 100%; padding: 6px 0 2px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; border-top: 1px solid var(--border); margin-top: 4px; }
  .filter-btn { background: var(--bg3); border: 1px solid var(--border); color: var(--dim); border-radius: 3px; padding: 3px 10px; font-size: 11px; cursor: pointer; font-family: inherit; transition: border-color 0.1s; }
  .filter-btn:hover { border-color: var(--cyan); color: var(--text); }
  .filter-btn.active { background: var(--cyan); color: #000; border-color: var(--cyan); font-weight: bold; }
</style>"""

_HTML_HEADER = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM Call Log</title>
{css}
</head>
<body data-last-call-ts="{last_call_ts_ms}">
<header>
  <h1>&#9654; LLM CALL LOG</h1>
  <span class="stats">{n_calls} call(s) &nbsp;|&nbsp; {n_ok} ok &nbsp;|&nbsp; {n_fail} failed &nbsp;|&nbsp; {n_parse_errors} parse error(s) &nbsp;|&nbsp; {total_s:.1f}s total</span>
  <span class="live-badge" id="live-badge">LIVE</span>
  <div class="legend">
    <span class="legend-title">Legend:</span>
    <span class="legend-item"><span class="badge badge-ok">&#10003; OK</span> &mdash; LLM call succeeded</span>
    <span class="legend-item"><span class="badge badge-fail">&#10007; FAILED</span> &mdash; LLM provider error (network / auth / timeout)</span>
    <span class="legend-item"><span class="badge badge-parse-error">&#9888; PARSE ERROR</span> &mdash; Response received but JSON parsing failed &mdash; findings for this call may be lost</span>
  </div>
  <div class="filter-bar">
    <span class="legend-title">Filter:</span>
    <button class="filter-btn" data-pass-filter="all">All</button>
    <button class="filter-btn" data-pass-filter="pass1">Pass 1 &mdash; PR Analysis</button>
    <button class="filter-btn" data-pass-filter="pass2">Pass 2 &mdash; Consolidation</button>
    <button class="filter-btn" data-pass-filter="pass2expand">Pass 2 &mdash; Expand</button>
    <button class="filter-btn" data-pass-filter="review">Cluster Review</button>
    <button class="filter-btn" data-pass-filter="chat">Chat</button>
    <button class="filter-btn" data-pass-filter="other">Other</button>
  </div>
</header>
<main>
"""

_HTML_FOOTER = """\
</main>
<footer>Matter RAG &mdash; LLM Call Log</footer>
<script>
(function () {
  var IDLE_MS    = 60000;          // stop refreshing if last call was >60 s ago
  var POLL_MS    = 5000;           // check every 5 s
  var KEY_OPEN   = 'llm-open-panes';
  var KEY_SCROLL = 'llm-scroll-y';

  // ── Restore open/scroll state immediately (prevents flash after reload) ──
  // Hide main while restoring open states, then reveal — avoids a visible
  // frame where all panes are closed before JS re-opens them.
  var mainEl = document.querySelector('main');
  if (mainEl) mainEl.style.visibility = 'hidden';
  try {
    var openIds = JSON.parse(sessionStorage.getItem(KEY_OPEN) || '[]');
    openIds.forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.setAttribute('open', '');
    });
    var sy = parseInt(sessionStorage.getItem(KEY_SCROLL) || '0', 10);
    if (sy) window.scrollTo(0, sy);
  } catch (e) {}
  if (mainEl) mainEl.style.visibility = '';

  // ── auto-refresh: always reload so new completed calls appear ────────────
  function lastCallAge() {
    var ts = parseInt(document.body.getAttribute('data-last-call-ts') || '0', 10);
    return ts ? Date.now() - ts : Infinity;
  }

  function markComplete() {
    var badge = document.getElementById('live-badge');
    if (badge) {
      badge.textContent = 'COMPLETE';
      badge.style.background = '#455a64';
      badge.style.color = '#b0bec5';
    }
  }

  if (lastCallAge() > IDLE_MS) {
    // page was loaded after run already completed — don't schedule refresh
    markComplete();
  } else {
    var timer = setInterval(function () {
      if (lastCallAge() > IDLE_MS) {
        clearInterval(timer);
        markComplete();
        return;
      }
      // Save open pane IDs and scroll position before reload so they survive
      var openIds = [];
      document.querySelectorAll('details[id][open]').forEach(function (el) {
        openIds.push(el.id);
      });
      try {
        sessionStorage.setItem(KEY_OPEN, JSON.stringify(openIds));
        sessionStorage.setItem(KEY_SCROLL, String(window.scrollY));
      } catch (e) {}
      location.reload();
    }, POLL_MS);
  }
}());

// ── Pass filter ───────────────────────────────────────────────────────────
(function () {
  var KEY_FILTER = 'llm-pass-filter';
  var activeFilter = 'all';
  try { activeFilter = sessionStorage.getItem(KEY_FILTER) || 'all'; } catch (e) {}

  function applyFilter(passKey) {
    activeFilter = passKey;
    try { sessionStorage.setItem(KEY_FILTER, passKey); } catch (e) {}
    document.querySelectorAll('.filter-btn').forEach(function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-pass-filter') === passKey);
    });
    document.querySelectorAll('.call-block, .event-block').forEach(function (el) {
      if (passKey === 'all') {
        el.style.display = '';
      } else {
        el.style.display = (el.getAttribute('data-pass') === passKey) ? '' : 'none';
      }
    });
  }

  document.querySelectorAll('.filter-btn').forEach(function (btn) {
    btn.addEventListener('click', function () { applyFilter(btn.getAttribute('data-pass-filter')); });
  });

  // Apply on initial load (restores filter after live-reload)
  applyFilter(activeFilter);
}());
</script>
</body>
</html>"""


def _esc(text: str) -> str:
    """HTML-escape a string for safe embedding inside <pre>."""
    return _html_mod.escape(text or "")


def _label_to_pass_key(label: str) -> str:
    """Map a call label to a filter key for the pass-filter toolbar."""
    low = label.lower()
    if low.startswith("pass 1"):
        return "pass1"
    if "consolidation" in low:
        return "pass2"
    if "expand" in low or low.startswith("pass 2") or low.startswith("pass 2/3"):
        return "pass2expand"
    if "cluster review" in low or "review" in low:
        return "review"
    if low.startswith("chat"):
        return "chat"
    return "other"


def _render_call_html(entry: dict, open_by_default: bool = False) -> str:
    """Render one LLM call entry as a <details> collapsible block."""
    call_id   = entry.get("call_id", "?")
    provider  = entry.get("provider", "unknown")
    label     = entry.get("label", "")
    ts        = entry.get("ts", "")
    duration  = entry.get("duration_s", 0.0)
    success   = entry.get("success", True)
    prompt    = entry.get("prompt", "")
    system    = entry.get("system", "")
    response  = entry.get("response", "")
    error     = entry.get("error", "")

    badge     = '<span class="badge badge-ok">&#10003; OK</span>' if success else '<span class="badge badge-fail">&#10007; FAILED</span>'
    open_attr = " open" if open_by_default else ""  # default open; JS may override via sessionStorage
    label_html = f' <span class="call-label">{_esc(label)}</span>' if label else ""
    pass_key  = _label_to_pass_key(label)

    system_section = ""
    if system:
        system_section = f"""
    <div>
      <div class="section-title">&#9654; SYSTEM PROMPT</div>
      <pre class="system">{_esc(system)}</pre>
    </div>"""

    error_section = ""
    if error:
        error_section = f"""
    <div>
      <div class="section-title section-arrow-out">&#10007; ERROR</div>
      <pre class="error">{_esc(error)}</pre>
    </div>"""

    response_section = f"""
    <div>
      <div class="section-title section-arrow-out">&lt;===== Response ====</div>
      <pre class="response">{_esc(response or "(no response)")}</pre>
    </div>"""

    return f"""<div class="call-block" data-pass="{pass_key}">
  <details id="call-{call_id}"{open_attr}>
    <summary>
      <span class="call-num">Call #{call_id}</span>
      <span class="call-provider">{_esc(provider)}</span>{label_html}
      <span class="call-duration">{duration:.2f}s</span>
      {badge}
      <span class="call-ts">{_esc(ts)}</span>
    </summary>
    <div class="call-body">
      <div class="meta-row">
        <span>prompt_len={len(prompt)}</span>
        <span>system_len={len(system or '')}</span>
        <span>response_len={len(response or '')}</span>
      </div>{system_section}
      <div>
        <div class="section-title section-arrow-in">===&gt; Sending Request ====</div>
        <pre class="request">{_esc(prompt)}</pre>
      </div>{response_section}{error_section}
    </div>
  </details>
</div>
"""
def _render_event_html(entry: dict) -> str:
    """Render a non-LLM event (e.g. parse_error) as an orange collapsible block."""
    event_type = entry.get("event_type", "event")
    context    = entry.get("context", "")
    detail     = entry.get("detail", "")
    ts         = entry.get("ts_display", entry.get("ts", ""))

    badge = '<span class="badge badge-parse-error">&#9888; PARSE ERROR</span>'
    label = f'<span class="event-context">{_esc(context)}</span>' if context else ""

    return f"""<div class="event-block" data-pass="other">
  <details id="event-{_esc(ts.replace(' ', '-').replace(':', '').replace('+', ''))}">
    <summary>
      {badge}
      {label}
      <span class="call-ts">{_esc(ts)}</span>
    </summary>
    <div class="call-body">
      <div class="section-title" style="color:var(--orange)">&#9888; Raw LLM response that failed JSON parsing (findings for this call are lost)</div>
      <pre class="parse-error-raw">{_esc(detail or "(no detail)")}</pre>
    </div>
  </details>
</div>
"""




class LLMCallLogger:
    """Dispatcher that writes every LLM call to three output sinks.

    Given ``base_path = "logs/llm_calls.jsonl"`` the three outputs are:

    * ``logs/llm_calls.jsonl`` — one JSON object per line
    * ``logs/llm_calls.txt``   — human-readable text with call delimiters
    * ``logs/llm_calls.html``  — dark-terminal HTML with collapsible panes

    Usage::

        call_logger = LLMCallLogger("logs/llm_calls.jsonl")
        call_logger.log(
            call_id=1, provider="ClaudeSubprocessProvider",
            prompt="...", system="...", response="...",
            duration_s=4.2, success=True, error=None,
        )
    """

    def __init__(self, base_path: str) -> None:
        global _active_logger
        p = Path(base_path)
        self._jsonl_path = p.with_suffix(".jsonl")
        self._txt_path   = p.with_suffix(".txt")
        self._html_path  = p.with_suffix(".html")
        for path in (self._jsonl_path, self._txt_path, self._html_path):
            path.parent.mkdir(parents=True, exist_ok=True)

        # Seed from existing JSONL so the HTML accumulates correctly when
        # get_llm() is called multiple times within the same run (once per node).
        self._entries: List[Dict] = self._load_existing_entries()
        # Label for the next logged call; set via set_next_label() and reset after use.
        self.next_call_label: str = ""
        _active_logger = self
        logger.info(
            "LLMCallLogger: jsonl=%s  txt=%s  html=%s  (prior_entries=%d)",
            self._jsonl_path, self._txt_path, self._html_path, len(self._entries),
        )

    def _load_existing_entries(self) -> List[Dict]:
        """Read back any entries already written to the JSONL file this run."""
        if not self._jsonl_path.exists():
            return []
        entries = []
        try:
            for line in self._jsonl_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    # Restore display timestamp if missing (older entries)
                    if "ts_display" not in entry:
                        entry["ts_display"] = entry.get("ts", "")
                    entries.append(entry)
        except Exception as exc:
            logger.warning("LLMCallLogger: could not read existing entries: %s", exc)
        return entries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log(
        self,
        call_id: int,
        provider: str,
        prompt: str,
        system: Optional[str],
        response: Optional[str],
        duration_s: float,
        success: bool,
        error: Optional[str],
        label: str = "",
    ) -> None:
        """Record one LLM call to all three output sinks."""
        effective_label = label or self.next_call_label
        self.next_call_label = ""  # reset after use
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        entry = {
            "call_id":    call_id,
            "ts":         datetime.now(timezone.utc).isoformat(),
            "ts_display": ts,
            "provider":   provider,
            "label":      effective_label,
            "prompt_len": len(prompt),
            "prompt":     prompt,
            "system":     system,
            "response":   response,
            "error":      error,
            "duration_s": round(duration_s, 3),
            "success":    success,
        }
        self._entries.append(entry)
        self._write_jsonl(entry)
        self._write_txt(entry)
        self._write_html()

    def log_event(self, event_type: str, context: str, detail: str) -> None:
        """Record a non-LLM event (e.g. JSON parse failure) in the HTML log.

        These are written to JSONL with an ``event_type`` field so they survive
        across ``LLMCallLogger`` re-instantiations within the same run.
        """
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        entry: Dict = {
            "event_type": event_type,
            "ts":         datetime.now(timezone.utc).isoformat(),
            "ts_display": ts,
            "context":    context,
            "detail":     detail,
        }
        self._entries.append(entry)
        try:
            with self._jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("LLMCallLogger JSONL event write failed: %s", exc)
        self._write_html()

    # ------------------------------------------------------------------
    # Sinks
    # ------------------------------------------------------------------

    def _write_jsonl(self, entry: dict) -> None:
        """Append one JSON line to the JSONL log (backward compat)."""
        try:
            # Write a subset — exclude ts_display (redundant with ts)
            row = {k: v for k, v in entry.items() if k != "ts_display"}
            with self._jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("LLMCallLogger JSONL write failed: %s", exc)

    def _write_txt(self, entry: dict) -> None:
        """Append a formatted call block to the text log."""
        call_id  = entry["call_id"]
        provider = entry["provider"]
        ts       = entry["ts_display"]
        duration = entry["duration_s"]
        success  = entry["success"]
        prompt   = entry.get("prompt", "")
        system   = entry.get("system", "")
        response = entry.get("response", "") or ""
        error    = entry.get("error", "") or ""

        status = "SUCCESS" if success else f"FAILED: {error}"
        banner = f"******** LLM Call #{call_id} ********"
        info   = f"[{ts}]  provider={provider}  duration={duration:.2f}s  {status}"

        lines = [
            "",
            _DIVIDER,
            banner,
            info,
            _DIVIDER,
        ]

        if system:
            lines += [
                "====> Sending Request ====",
                "[SYSTEM]",
                system,
                "",
                "[USER]",
                prompt,
            ]
        else:
            lines += [
                "====> Sending Request ====",
                prompt,
            ]

        lines += [
            "",
            "<===== Response ====",
            response if success else f"ERROR: {error}",
            _DIVIDER,
            "",
        ]

        try:
            with self._txt_path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as exc:
            logger.warning("LLMCallLogger TXT write failed: %s", exc)

    def _write_html(self) -> None:
        """Rewrite the complete HTML file from the in-memory entries list."""
        try:
            entries  = self._entries
            call_entries = [e for e in entries if "event_type" not in e]
            n_calls  = len(call_entries)
            n_ok     = sum(1 for e in call_entries if e.get("success", True))
            n_fail   = n_calls - n_ok
            total_s  = sum(e.get("duration_s", 0.0) for e in call_entries)
            n_parse_errors = sum(1 for e in entries if e.get("event_type") == "parse_error")

            # Unix-ms timestamp of the most-recent call (used by JS idle detector)
            if call_entries:
                try:
                    last_dt = datetime.fromisoformat(call_entries[-1]["ts"].replace("Z", "+00:00"))
                    last_call_ts_ms = int(last_dt.timestamp() * 1000)
                except Exception:
                    last_call_ts_ms = 0
            else:
                last_call_ts_ms = 0

            header = _HTML_HEADER.format(
                css=_HTML_CSS,
                n_calls=n_calls,
                n_ok=n_ok,
                n_fail=n_fail,
                n_parse_errors=n_parse_errors,
                total_s=total_s,
                last_call_ts_ms=last_call_ts_ms,
            )

            # Render entries in order; dispatch on type; last LLM call open by default
            last_call_idx = next(
                (i for i in range(len(entries) - 1, -1, -1) if "event_type" not in entries[i]),
                -1,
            )
            blocks = []
            for i, e in enumerate(entries):
                if "event_type" in e:
                    blocks.append(_render_event_html(e))
                else:
                    blocks.append(_render_call_html(e, open_by_default=(i == last_call_idx)))

            self._html_path.write_text(
                header + "".join(blocks) + _HTML_FOOTER,
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("LLMCallLogger HTML write failed: %s", exc)
