"""Spec Diff Builder — generates diff HTML from a spec PR via Docker make.

Given a GitHub PR URL pointing to the spec repo, this module:
1. Fetches PR metadata (base SHA, head SHA, changed files)
2. Ensures a local clone of the spec repo exists
3. Checks out the PR head commit
4. Extracts in-progress feature flags from changed adoc files
5. Runs `make html-diff-all` via Docker to produce diff HTML
6. Copies the diff HTMLs to the output directory
7. Restores the spec repo to its previous branch

Requires:
- Docker Desktop running
- GITHUB_TOKEN env var (for private repos)
- Local disk space for the spec repo clone (~2GB)

Usage from code::

    from src.fetcher.spec_diff_builder import build_spec_diff
    diff_files = build_spec_diff(
        pr_url="https://github.com/CHIP-Specifications/connectedhomeip-spec/pull/12949",
        config=app_config,
        output_dir=Path("data/input_doc"),
        log_dir=Path("logs/my_run"),
    )
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Ensure spec_diff_builder messages always show timestamps on console
# (this module has long-running steps where users watch progress)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
    logger.propagate = True

_IFDEF_FLAG_RE = re.compile(r'ifdef::in-progress,([a-zA-Z0-9_-]+)')


def build_spec_diff(
    pr_url: str,
    config,
    output_dir: Path,
    log_dir: Optional[Path] = None,
    token: str = "",
) -> List[str]:
    """End-to-end: PR URL → diff HTML files in output_dir.

    Args:
        pr_url: GitHub PR URL (e.g., https://github.com/org/repo/pull/123)
        config: AppConfig with spec_repo settings
        output_dir: directory to write *_diff.html files
        log_dir: directory for build logs (make stdout/stderr)
        token: GitHub token for API + git auth (falls back to GITHUB_TOKEN env)

    Returns:
        List of paths to generated diff HTML files.
        Empty list if no adoc files changed or build failed.
    """
    token = token or os.environ.get("GITHUB_TOKEN", "")

    # ── Step 1: Parse PR URL and fetch metadata ──────────────────────────
    owner, repo, pr_number = _parse_pr_url(pr_url)
    pr_meta = _fetch_pr_metadata(owner, repo, pr_number, token)
    changed_adocs = _fetch_pr_changed_adoc_files(owner, repo, pr_number, token)

    if not changed_adocs:
        logger.warning("[spec_diff_builder] No .adoc files changed in PR #%d — nothing to generate", pr_number)
        return []

    # ── Step 2: Ensure spec repo ─────────────────────────────────────────
    spec_repo_path = Path(config.spec_repo.path) if config.spec_repo.path else None
    spec_repo_url = f"https://github.com/{owner}/{repo}.git"

    if spec_repo_path and spec_repo_path.exists() and (spec_repo_path / ".git").exists():
        logger.info("[spec_diff_builder] Using existing spec repo: %s", spec_repo_path)
        subprocess.run(["git", "fetch", "origin"], cwd=str(spec_repo_path), check=True, capture_output=True)
    elif spec_repo_path:
        logger.info("[spec_diff_builder] Cloning spec repo to configured path: %s", spec_repo_path)
        _clone_repo(spec_repo_url, spec_repo_path, token)
    else:
        spec_repo_path = Path(tempfile.mkdtemp(prefix="matter-spec-"))
        logger.info("[spec_diff_builder] No spec_repo.path configured — cloning to temp: %s", spec_repo_path)
        _clone_repo(spec_repo_url, spec_repo_path, token)

    # Fetch PR ref
    try:
        subprocess.run(
            ["git", "fetch", "origin", f"pull/{pr_number}/head:pr-{pr_number}"],
            cwd=str(spec_repo_path), check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("[spec_diff_builder] Failed to fetch PR ref: %s", exc)
        return []

    # Checkout PR head
    prev_ref = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(spec_repo_path), capture_output=True, text=True,
    ).stdout.strip()

    logger.info("[spec_diff_builder] Checking out PR head: %s", pr_meta["head_sha"][:10])
    subprocess.run(
        ["git", "checkout", "--detach", pr_meta["head_sha"]],
        cwd=str(spec_repo_path), check=True, capture_output=True,
    )

    try:
        # ── Step 3: Extract feature flags ────────────────────────────────
        flags = _extract_feature_flags(spec_repo_path, changed_adocs)

        # ── Step 4: Compute merge base ───────────────────────────────────
        base_sha = ""
        for remote_branch in ["origin/master", "origin/main"]:
            try:
                merge_base_result = subprocess.run(
                    ["git", "merge-base", remote_branch, pr_meta["head_sha"]],
                    cwd=str(spec_repo_path), capture_output=True, text=True, check=True,
                )
                base_sha = merge_base_result.stdout.strip()
                logger.info("[spec_diff_builder] Merge base (%s): %s", remote_branch, base_sha[:10])
                break
            except subprocess.CalledProcessError:
                continue
        if not base_sha:
            base_sha = pr_meta["base_sha"]
            logger.warning("[spec_diff_builder] Could not compute merge-base — using API base: %s", base_sha[:10])

        # ── Step 5: Docker check ─────────────────────────────────────────
        if not _check_docker():
            logger.error(
                "[spec_diff_builder] Docker is not running. "
                "The spec build requires Docker Desktop. Start Docker and try again."
            )
            return []

        # ── Step 6: Run make ─────────────────────────────────────────────
        rc = _run_make(spec_repo_path, base_sha, flags, log_dir)
        if rc != 0:
            return []

        # ── Step 7: Copy diff HTMLs ──────────────────────────────────────
        return _copy_diff_htmls(spec_repo_path, output_dir)

    finally:
        # Restore spec repo
        try:
            subprocess.run(
                ["git", "checkout", prev_ref],
                cwd=str(spec_repo_path), check=True, capture_output=True,
            )
            logger.info("[spec_diff_builder] Restored spec repo to: %s", prev_ref)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_pr_url(pr_url: str) -> Tuple[str, str, int]:
    m = re.match(r'https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)', pr_url)
    if not m:
        raise ValueError(f"Invalid PR URL: {pr_url}")
    return m.group(1), m.group(2), int(m.group(3))


def _fetch_pr_metadata(owner: str, repo: str, pr_number: int, token: str) -> Dict:
    import urllib.request

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github.v3+json")

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    base_sha = data.get("base", {}).get("sha", "")
    head_sha = data.get("head", {}).get("sha", "")
    title = data.get("title", "")

    logger.info("[spec_diff_builder] PR #%d: %s", pr_number, title)
    logger.info("[spec_diff_builder]   base=%s  head=%s", base_sha[:10], head_sha[:10])

    return {"base_sha": base_sha, "head_sha": head_sha, "title": title}


def _fetch_pr_changed_adoc_files(owner: str, repo: str, pr_number: int, token: str) -> List[str]:
    import urllib.request

    files = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100&page={page}"
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github.v3+json")

        with urllib.request.urlopen(req, timeout=30) as resp:
            page_data = json.loads(resp.read().decode())

        if not page_data:
            break
        files.extend([f["filename"] for f in page_data])
        if len(page_data) < 100:
            break
        page += 1

    adoc_files = [f for f in files if f.endswith(".adoc")]
    logger.info("[spec_diff_builder]   Changed files: %d total, %d .adoc", len(files), len(adoc_files))
    return adoc_files


def _clone_repo(url: str, dest: Path, token: str) -> None:
    clone_url = url
    if token and "github.com" in clone_url and "@" not in clone_url:
        clone_url = clone_url.replace("https://github.com", f"https://x-access-token:{token}@github.com")
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", clone_url, str(dest)], check=True)


def _extract_feature_flags(spec_repo_path: Path, changed_adoc_files: List[str]) -> List[str]:
    flags = set()
    for rel_path in changed_adoc_files:
        full_path = spec_repo_path / rel_path
        if not full_path.exists():
            continue
        try:
            text = full_path.read_text(encoding="utf-8", errors="replace")
            for m in _IFDEF_FLAG_RE.finditer(text):
                flag = m.group(1)
                if flag != "in-progress":
                    flags.add(flag)
        except Exception:
            pass

    sorted_flags = sorted(flags)
    logger.info("[spec_diff_builder] Feature flags: %s", " ".join(sorted_flags) or "(none)")
    return sorted_flags


def _check_docker() -> bool:
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run_make(spec_repo_path: Path, base_sha: str, flags: List[str], log_dir: Optional[Path]) -> int:
    import time

    # Patch ALL Makefiles to downgrade --failure-level=INFO to WARNING so that
    # unrelated broken cross-references (asciidoctor INFO messages) don't abort
    # the build. The diff target creates build/base/ with its own Makefile from
    # the base commit, so we use a wrapper script with a background watcher.
    makefile_path = spec_repo_path / "Makefile"
    makefile_patched = False
    makefile_backup = None
    if makefile_path.is_file():
        original = makefile_path.read_text(encoding="utf-8")
        if "--failure-level=INFO" in original:
            makefile_backup = original
            patched = original.replace("--failure-level=INFO", "--failure-level=WARNING")
            makefile_path.write_text(patched, encoding="utf-8")
            makefile_patched = True
            logger.debug("[spec_diff_builder] Patched Makefile: --failure-level=WARNING")

    cmd = ["make", "ENABLE_PARAGRAPH_NUMBERING=1"]
    if flags:
        cmd.append(f'INCLUDE_IN_PROGRESS={" ".join(flags)}')
    cmd.extend(["html", "html-appclusters-book", "html-diff", "html-diff-appclusters", f"BASE={base_sha}"])

    # Build a properly shell-quoted command string for the wrapper script
    import shlex
    make_cmd_str = " ".join(shlex.quote(c) for c in cmd)
    wrapper_script = (
        "#!/bin/bash\n"
        "# Background watcher: patch build/base/Makefile as soon as it appears\n"
        "(\n"
        "  for i in $(seq 1 120); do\n"
        "    if [ -f build/base/Makefile ]; then\n"
        "      sed -i.bak 's/--failure-level=INFO/--failure-level=WARNING/g' build/base/Makefile 2>/dev/null\n"
        "      rm -f build/base/Makefile.bak\n"
        "      break\n"
        "    fi\n"
        "    sleep 0.5\n"
        "  done\n"
        ") &\n"
        "WATCHER_PID=$!\n"
        f"{make_cmd_str}\n"
        "MAKE_RC=$?\n"
        "kill $WATCHER_PID 2>/dev/null\n"
        "wait $WATCHER_PID 2>/dev/null\n"
        "exit $MAKE_RC\n"
    )
    wrapper_path = spec_repo_path / ".make_wrapper.sh"
    wrapper_path.write_text(wrapper_script, encoding="utf-8")
    wrapper_path.chmod(0o755)

    logger.info("[spec_diff_builder] Running: %s", make_cmd_str)
    logger.info("[spec_diff_builder]   (this takes 15-30 minutes)")

    # Stream make output to both console and log file so progress is visible
    # and failures are diagnosable from either location.
    log_path = None
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "spec_diff_build.log"

    t0 = time.time()
    if log_path:
        # Use tee: write to file AND stream to console
        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(
                ["bash", str(wrapper_path)], cwd=str(spec_repo_path),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            for line in proc.stdout:
                decoded = line.decode("utf-8", errors="replace")
                log_file.write(decoded)
                print(decoded, end="", flush=True)
            proc.wait()
            result_code = proc.returncode
    else:
        result_obj = subprocess.run(
            ["bash", str(wrapper_path)], cwd=str(spec_repo_path),
        )
        result_code = result_obj.returncode
    elapsed = time.time() - t0

    # Cleanup
    wrapper_path.unlink(missing_ok=True)
    if makefile_patched and makefile_backup is not None:
        makefile_path.write_text(makefile_backup, encoding="utf-8")
        logger.debug("[spec_diff_builder] Restored original Makefile")

    if result_code != 0:
        logger.error("[spec_diff_builder] Make failed (exit %d) after %.0fs", result_code, elapsed)
        if log_path:
            logger.error("[spec_diff_builder] See: %s", log_path)
        return result_code

    logger.info("[spec_diff_builder] Make completed in %.0fs", elapsed)
    return 0


def _copy_diff_htmls(spec_repo_path: Path, output_dir: Path) -> List[str]:
    build_dir = spec_repo_path / "build" / "html"
    diff_files = sorted(build_dir.glob("*_diff.html"))

    if not diff_files:
        logger.error("[spec_diff_builder] No diff HTML files found in %s", build_dir)
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for f in diff_files:
        dest = output_dir / f.name
        shutil.copy2(f, dest)
        copied.append(str(dest))
        logger.info("[spec_diff_builder]   → %s", f.name)

    logger.info("[spec_diff_builder] %d diff HTML file(s) written to %s", len(copied), output_dir)
    return copied
