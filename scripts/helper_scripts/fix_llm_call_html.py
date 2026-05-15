#!/usr/bin/env python3
"""Patch existing llm_calls.html files to remove the auto-refresh meta tag
and inject the JS-based smart-refresh + sessionStorage pane-state fix.

Usage:
    # Fix all llm_calls.html files under logs/
    python scripts/fix_llm_call_html.py

    # Fix a specific file
    python scripts/fix_llm_call_html.py logs/ghpr_analysis_04172026_173944/llm_calls.html
"""

from __future__ import annotations
import re
import sys
from pathlib import Path

_META_RE = re.compile(
    r'\s*<meta\s+http-equiv=["\']refresh["\']\s+content=["\'][^"\']*["\']\s*>\s*',
    re.IGNORECASE,
)

_FOOTER_OLD_RE = re.compile(
    r'<footer>[^<]*auto-refreshes[^<]*</footer>',
    re.IGNORECASE,
)

# JS block to inject just before </body>
_JS_BLOCK = """\
<script>
(function () {
  // ── sessionStorage: restore open/closed state before first paint ──────────
  document.querySelectorAll('details[id]').forEach(function (el) {
    var key = 'llm-call-open:' + el.id;
    var saved = sessionStorage.getItem(key);
    if (saved === 'true')  el.setAttribute('open', '');
    if (saved === 'false') el.removeAttribute('open');
    el.addEventListener('toggle', function () {
      sessionStorage.setItem(key, el.open ? 'true' : 'false');
    });
  });

  // ── smart auto-refresh: stops when run is complete ────────────────────────
  var IDLE_MS = 60000;
  var POLL_MS = 5000;

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

  // For already-completed runs (no data-last-call-ts), just mark complete.
  if (lastCallAge() > IDLE_MS) {
    markComplete();
  } else {
    var timer = setInterval(function () {
      if (lastCallAge() > IDLE_MS) {
        clearInterval(timer);
        markComplete();
        return;
      }
      document.querySelectorAll('details[id]').forEach(function (el) {
        sessionStorage.setItem('llm-call-open:' + el.id, el.open ? 'true' : 'false');
      });
      location.reload();
    }, POLL_MS);
  }
}());
</script>"""


def patch_html(path: Path) -> bool:
    """Patch one HTML file. Returns True if the file was modified."""
    text = path.read_text(encoding="utf-8")

    original = text

    # 1. Remove <meta http-equiv="refresh"> line
    text = _META_RE.sub("\n", text)

    # 2. Fix footer text (remove "auto-refreshes every 5s")
    text = _FOOTER_OLD_RE.sub(
        "<footer>Matter RAG &mdash; LLM Call Log</footer>", text
    )

    # 3. Add id="live-badge" to the live badge span if missing
    text = text.replace(
        '<span class="live-badge">LIVE</span>',
        '<span class="live-badge" id="live-badge">LIVE</span>',
    )

    # 4. Inject JS before </body> (only if not already present)
    if "_JS_BLOCK" not in text and "llm-call-open:" not in text:
        text = text.replace("</body>", _JS_BLOCK + "\n</body>")

    if text == original:
        return False

    path.write_text(text, encoding="utf-8")
    return True


def main() -> None:
    if len(sys.argv) > 1:
        targets = [Path(p) for p in sys.argv[1:]]
    else:
        root = Path(__file__).resolve().parent.parent.parent / "logs"
        targets = sorted(root.rglob("llm_calls.html"))

    if not targets:
        print("No llm_calls.html files found.")
        return

    for path in targets:
        if not path.exists():
            print(f"  NOT FOUND: {path}")
            continue
        changed = patch_html(path)
        status = "patched" if changed else "already up-to-date"
        print(f"  {status}: {path}")


if __name__ == "__main__":
    main()
