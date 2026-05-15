#!/usr/bin/env python3
"""Generate diff HTML from a Matter spec PR and optionally run TC generation.

Fetches a GitHub PR, checks out the spec repo, extracts in-progress feature
flags from changed adoc files, runs the Docker-based Asciidoctor build to
produce diff HTML, and optionally feeds it into the analysis pipeline.

Prerequisites:
  - Docker Desktop installed and running
  - GITHUB_TOKEN env var set (for PR metadata fetch)
  - Internet access (to clone spec repo on first run)

Usage:
    # Full flow: PR → diff HTML → test cases
    python scripts/generate_spec_diff.py \\
        --pr-url https://github.com/project-chip/connectedhomeip/pull/12345

    # Just generate diff HTML (no TC generation)
    python scripts/generate_spec_diff.py \\
        --pr-url https://github.com/project-chip/connectedhomeip/pull/12345 \\
        --diff-only

    # Use specific spec repo clone
    python scripts/generate_spec_diff.py \\
        --pr-url https://github.com/project-chip/connectedhomeip/pull/12345 \\
        --spec-repo /path/to/connectedhomeip-spec
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_IFDEF_FLAG_RE = re.compile(r'ifdef::in-progress,([a-zA-Z0-9_-]+)')


# ---------------------------------------------------------------------------
# Step 1: Parse PR URL and fetch metadata
# ---------------------------------------------------------------------------

def parse_pr_url(pr_url: str) -> tuple:
    """Extract owner, repo, PR number from a GitHub PR URL."""
    m = re.match(r'https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)', pr_url)
    if not m:
        logger.error("Invalid PR URL: %s", pr_url)
        sys.exit(1)
    return m.group(1), m.group(2), int(m.group(3))


def fetch_pr_metadata(owner: str, repo: str, pr_number: int, token: str) -> dict:
    """Fetch PR metadata from GitHub API."""
    import urllib.request
    import json

    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github.v3+json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        logger.error("Failed to fetch PR metadata: %s", exc)
        sys.exit(1)

    base_sha = data.get("base", {}).get("sha", "")
    head_sha = data.get("head", {}).get("sha", "")
    head_ref = data.get("head", {}).get("ref", "")
    title = data.get("title", "")

    logger.info("PR #%d: %s", pr_number, title)
    logger.info("  base: %s  head: %s  branch: %s", base_sha[:10], head_sha[:10], head_ref)

    return {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "head_ref": head_ref,
        "title": title,
    }


def fetch_pr_changed_files(owner: str, repo: str, pr_number: int, token: str) -> list:
    """Fetch list of changed files from the PR."""
    import urllib.request
    import json

    files = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=100&page={page}"
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github.v3+json")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                page_data = json.loads(resp.read().decode())
        except Exception as exc:
            logger.error("Failed to fetch PR files (page %d): %s", page, exc)
            break

        if not page_data:
            break
        files.extend([f["filename"] for f in page_data])
        if len(page_data) < 100:
            break
        page += 1

    adoc_files = [f for f in files if f.endswith(".adoc")]
    logger.info("  Changed files: %d total, %d .adoc", len(files), len(adoc_files))
    return adoc_files


# ---------------------------------------------------------------------------
# Step 2: Ensure spec repo exists
# ---------------------------------------------------------------------------

def ensure_spec_repo(spec_repo_path: Path, spec_repo_url: str, use_docker_image: bool = False, token: str = "") -> Path:
    """Clone or update the spec repo.

    If spec_repo_path is empty/None, clones to a temp directory.
    If use_docker_image is True, pulls the spec Docker image and extracts the repo.
    Uses GITHUB_TOKEN for authentication when cloning private repos.
    """
    if use_docker_image:
        return _ensure_spec_repo_from_docker(spec_repo_url)

    if spec_repo_path.exists() and (spec_repo_path / ".git").exists():
        logger.info("Spec repo found at %s — fetching latest", spec_repo_path)
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=str(spec_repo_path), check=True, capture_output=True,
        )
        return spec_repo_path

    # Inject token into URL for private repo auth
    clone_url = spec_repo_url
    if token and "github.com" in clone_url and "@" not in clone_url:
        clone_url = clone_url.replace("https://github.com", f"https://x-access-token:{token}@github.com")

    logger.info("Cloning spec repo from %s to %s", spec_repo_url, spec_repo_path)
    logger.info("  (this may take several minutes on first run)")
    spec_repo_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", clone_url, str(spec_repo_path)],
        check=True,
    )
    return spec_repo_path


def _ensure_spec_repo_from_docker(spec_repo_url: str) -> Path:
    """Pull the spec Docker image and extract the repo from it.

    The Docker image is expected to contain a git clone of the spec repo
    at /documents/. The image tag encodes the commit ID.
    """
    import tempfile

    _DOCKER_SPEC_IMAGE = os.environ.get(
        "SPEC_DOCKER_IMAGE",
        "ghcr.io/project-chip/connectedhomeip-spec:latest",
    )
    logger.info("Pulling spec Docker image: %s", _DOCKER_SPEC_IMAGE)

    rc = subprocess.run(["docker", "pull", _DOCKER_SPEC_IMAGE], capture_output=True)
    if rc.returncode != 0:
        logger.error(
            "Failed to pull spec Docker image: %s\n"
            "Set SPEC_DOCKER_IMAGE env var to override the image name.\n"
            "Or use --spec-repo to point to a local clone instead.",
            rc.stderr.decode()[:200],
        )
        sys.exit(1)

    # Extract the spec repo from the Docker image to a temp directory
    temp_dir = Path(tempfile.mkdtemp(prefix="matter-spec-docker-"))
    logger.info("Extracting spec repo from Docker image to %s", temp_dir)

    container_id = subprocess.run(
        ["docker", "create", _DOCKER_SPEC_IMAGE],
        capture_output=True, text=True,
    ).stdout.strip()

    try:
        subprocess.run(
            ["docker", "cp", f"{container_id}:/documents/.", str(temp_dir)],
            check=True, capture_output=True,
        )
    finally:
        subprocess.run(["docker", "rm", container_id], capture_output=True)

    if not (temp_dir / ".git").exists():
        logger.error(
            "Docker image does not contain a git repo at /documents/.\n"
            "The image must have the spec repo cloned at /documents/ for diff generation."
        )
        sys.exit(1)

    logger.info("Spec repo extracted from Docker image (%s)", temp_dir)
    return temp_dir


def checkout_pr_head(spec_repo_path: Path, head_sha: str) -> str:
    """Checkout the PR head commit. Returns the previous ref for restore."""
    prev_ref = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(spec_repo_path), capture_output=True, text=True,
    ).stdout.strip()

    logger.info("Checking out PR head: %s", head_sha[:10])
    subprocess.run(
        ["git", "checkout", "--detach", head_sha],
        cwd=str(spec_repo_path), check=True, capture_output=True,
    )
    return prev_ref


def restore_spec_repo(spec_repo_path: Path, prev_ref: str) -> None:
    """Restore spec repo to previous branch."""
    try:
        subprocess.run(
            ["git", "checkout", prev_ref],
            cwd=str(spec_repo_path), check=True, capture_output=True,
        )
        logger.info("Restored spec repo to: %s", prev_ref)
    except Exception as exc:
        logger.warning("Could not restore spec repo to %s: %s", prev_ref, exc)


# ---------------------------------------------------------------------------
# Step 3: Extract in-progress feature flags
# ---------------------------------------------------------------------------

def extract_feature_flags(spec_repo_path: Path, changed_adoc_files: list) -> list:
    """Scan changed adoc files for ifdef::in-progress,<flag> patterns."""
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
        except Exception as exc:
            logger.warning("Could not read %s: %s", rel_path, exc)

    sorted_flags = sorted(flags)
    logger.info("Extracted %d feature flag(s): %s", len(sorted_flags), " ".join(sorted_flags))
    return sorted_flags


# ---------------------------------------------------------------------------
# Step 4: Docker pre-check and make
# ---------------------------------------------------------------------------

def check_docker() -> bool:
    """Verify Docker is running."""
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def run_make_diff(spec_repo_path: Path, base_sha: str, flags: list, docker_image: str) -> int:
    """Run the make command to generate diff HTML."""
    include_in_progress = " ".join(flags) if flags else ""

    cmd = [
        "make",
        "ENABLE_PARAGRAPH_NUMBERING=1",
    ]
    if include_in_progress:
        cmd.append(f'INCLUDE_IN_PROGRESS={include_in_progress}')

    cmd.extend([
        "html",
        "html-appclusters-book",
        "html-diff",
        "html-diff-appclusters",
        f"BASE={base_sha}",
    ])

    logger.info("Running: %s", " ".join(cmd))
    logger.info("  (this takes 15-30 minutes — building HTML via Docker)")

    t0 = time.time()
    result = subprocess.run(cmd, cwd=str(spec_repo_path))
    elapsed = time.time() - t0

    if result.returncode != 0:
        logger.error("Make failed (exit code %d) after %.0fs", result.returncode, elapsed)
        return result.returncode

    logger.info("Make completed in %.0fs", elapsed)
    return 0


# ---------------------------------------------------------------------------
# Step 5: Copy diff HTMLs to output
# ---------------------------------------------------------------------------

def copy_diff_htmls(spec_repo_path: Path, output_dir: Path) -> list:
    """Copy all *_diff.html files from build/html/ to output directory."""
    build_dir = spec_repo_path / "build" / "html"
    diff_files = sorted(build_dir.glob("*_diff.html"))

    if not diff_files:
        logger.error("No diff HTML files found in %s", build_dir)
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for f in diff_files:
        dest = output_dir / f.name
        shutil.copy2(f, dest)
        copied.append(str(dest))
        logger.info("  Copied: %s", f.name)

    logger.info("Copied %d diff HTML file(s) to %s", len(copied), output_dir)
    return copied


# ---------------------------------------------------------------------------
# Step 6: Run pipeline
# ---------------------------------------------------------------------------

def run_pipeline(output_dir: Path, additional_config: str, config_path: str) -> int:
    """Run the analysis pipeline on the generated diff HTMLs."""
    cmd = [
        sys.executable,
        "scripts/run_ghpr_analysis.py",
        "--compare-only",
        "--input-doc-dir", str(output_dir),
        "--auto-detect-clusters",
    ]
    if additional_config:
        cmd += ["--additional-config", additional_config]
    if config_path != "config/config.yaml":
        cmd += ["--config", config_path]

    logger.info("Running analysis pipeline on %s", output_dir)
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    return result.returncode


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate diff HTML from a Matter spec PR and run TC generation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--pr-url", required=True,
        help="GitHub PR URL (e.g., https://github.com/project-chip/connectedhomeip/pull/12345)",
    )
    parser.add_argument(
        "--spec-repo", metavar="DIR", default="",
        help="Path to local spec repo clone (overrides config.spec_repo.path)",
    )
    parser.add_argument(
        "--output-dir", metavar="DIR", default="",
        help="Output directory for diff HTMLs (default: data/input_doc/)",
    )
    parser.add_argument(
        "--diff-only", action="store_true",
        help="Only generate diff HTML — do not run the analysis pipeline",
    )
    parser.add_argument(
        "--config", default="config/config.yaml",
        help="Path to config YAML (default: config/config.yaml)",
    )
    parser.add_argument(
        "--additional-config", metavar="FILE", default="",
        help="Overlay config YAML (deep-merged on top of base config)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load config
    from src.config.config_loader import load_config
    config = load_config(args.config, additional_config=args.additional_config or None)

    # GitHub token
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        logger.warning("GITHUB_TOKEN not set — API rate limits apply (60 req/hr)")

    # ── Step 1: Parse PR URL and fetch metadata ──────────────────────────
    owner, repo, pr_number = parse_pr_url(args.pr_url)
    pr_meta = fetch_pr_metadata(owner, repo, pr_number, token)
    changed_adocs = fetch_pr_changed_files(owner, repo, pr_number, token)

    if not changed_adocs:
        logger.warning("No .adoc files changed in this PR — nothing to generate")
        return 0

    # ── Step 2: Ensure spec repo ──────────────────────────────────────────
    _DOCKER_SPEC_MODE = "connectedhomeip-pr-spec-docker"
    spec_repo_cfg = args.spec_repo or config.spec_repo.path or ""
    # Auto-derive repo URL from PR URL (more reliable than config)
    spec_repo_url = f"https://github.com/{owner}/{repo}.git"
    use_docker_image = spec_repo_cfg.strip().lower() == _DOCKER_SPEC_MODE

    if use_docker_image:
        logger.info("Docker spec image mode — pulling pre-built spec image")
        spec_repo_path = ensure_spec_repo(Path("/unused"), spec_repo_url, use_docker_image=True)
    elif spec_repo_cfg:
        spec_repo_path = Path(spec_repo_cfg).resolve()
        spec_repo_path = ensure_spec_repo(spec_repo_path, spec_repo_url, token=token)
    else:
        # No path configured — clone to temp directory
        import tempfile
        temp_dir = Path(tempfile.mkdtemp(prefix="matter-spec-"))
        logger.info("No spec_repo.path configured — cloning to temp: %s", temp_dir)
        spec_repo_path = ensure_spec_repo(temp_dir, spec_repo_url, token=token)

    # Fetch the PR ref so we can checkout
    try:
        subprocess.run(
            ["git", "fetch", "origin", f"pull/{pr_number}/head:pr-{pr_number}"],
            cwd=str(spec_repo_path), check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to fetch PR ref: %s", exc)
        return 1

    prev_ref = checkout_pr_head(spec_repo_path, pr_meta["head_sha"])

    try:
        # ── Step 3: Extract feature flags ─────────────────────────────────
        flags = extract_feature_flags(spec_repo_path, changed_adocs)

        # Compute the true merge base locally (more accurate than GitHub API's base.sha)
        try:
            merge_base_result = subprocess.run(
                ["git", "merge-base", "origin/main", pr_meta["head_sha"]],
                cwd=str(spec_repo_path), capture_output=True, text=True, check=True,
            )
            base_sha = merge_base_result.stdout.strip()
            logger.info("Computed merge base: %s (via git merge-base origin/main %s)",
                        base_sha[:10], pr_meta["head_sha"][:10])
        except subprocess.CalledProcessError:
            base_sha = pr_meta["base_sha"]
            logger.warning("Could not compute merge-base — falling back to GitHub API base: %s", base_sha[:10])

        # ── Step 4: Docker check + make ───────────────────────────────────
        if not check_docker():
            logger.error(
                "Docker is not running. The spec build requires Docker Desktop.\n"
                "Start Docker and try again."
            )
            return 1

        docker_image = config.spec_repo.docker_image
        rc = run_make_diff(spec_repo_path, base_sha, flags, docker_image)
        if rc != 0:
            return rc

        # ── Step 5: Copy diff HTMLs ───────────────────────────────────────
        output_dir = Path(args.output_dir or str(PROJECT_ROOT / "data" / "input_doc")).resolve()
        copied = copy_diff_htmls(spec_repo_path, output_dir)
        if not copied:
            return 1

    finally:
        # ── Restore spec repo ─────────────────────────────────────────────
        restore_spec_repo(spec_repo_path, prev_ref)

    # ── Step 6: Run pipeline ──────────────────────────────────────────────
    if args.diff_only:
        logger.info("Diff-only mode — skipping pipeline")
        print(f"\nDiff HTMLs ready at: {output_dir}")
        print(f"Run the pipeline with:")
        print(f"  python scripts/run_ghpr_analysis.py --compare-only \\")
        print(f"    --input-doc-dir {output_dir} --auto-detect-clusters")
        return 0

    rc = run_pipeline(output_dir, args.additional_config, args.config)
    return rc


if __name__ == "__main__":
    sys.exit(main())
