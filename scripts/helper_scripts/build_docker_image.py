#!/usr/bin/env python3
"""Build Docker Image — downloads spec, DM XMLs, test plans, builds KG + FAISS, packages into Docker.

Creates a Docker image containing everything the pipeline needs to analyze PRs:
  - Spec repo at a known commit (with built HTML)
  - DM XML cluster files from the SDK repo
  - Test plan HTML files from the test plans repo
  - Pre-built Knowledge Graph (KG JSON)
  - Pre-built FAISS vector DB (index + metadata)
  - BGE embedding model (cached)
  - Pipeline code + Python dependencies
  - manifest.json with commit SHAs for diff generation

Run nightly to keep the base image current. Per-PR analysis pulls this image
and uses its data + manifest commit as the diff BASE.

Usage:
    # Build and push to Docker Hub
    python scripts/helper_scripts/build_docker_image.py \\
        --push myregistry/matter-rag-base:latest

    # Build with custom branches
    python scripts/helper_scripts/build_docker_image.py \\
        --spec-branch main --sdk-branch master \\
        --push myregistry.com/org/matter-rag-base:latest

    # Build only (no push)
    python scripts/helper_scripts/build_docker_image.py \\
        --tag matter-rag-base:local

    # Build data only (no Docker image)
    python scripts/helper_scripts/build_docker_image.py --no-docker
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _header(msg: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {msg}")
    print(f"{'=' * 60}\n")


def _run(cmd: list, cwd: str = None, check: bool = True) -> subprocess.CompletedProcess:
    logger.debug("Running: %s", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, cwd=cwd, check=check)


def _clone_or_pull(repo_url: str, dest: Path, branch: str, token: str = "") -> str:
    """Clone or update a repo. Returns the HEAD commit SHA."""
    clone_url = repo_url
    if token and "github.com" in clone_url and "@" not in clone_url:
        clone_url = clone_url.replace("https://github.com", f"https://x-access-token:{token}@github.com")

    if dest.exists() and (dest / ".git").exists():
        logger.info("Updating existing clone: %s", dest)
        subprocess.run(["git", "fetch", "origin"], cwd=str(dest), check=True, capture_output=True)
        subprocess.run(["git", "checkout", branch], cwd=str(dest), check=True, capture_output=True)
        subprocess.run(["git", "pull", "origin", branch], cwd=str(dest), check=True, capture_output=True)
    else:
        logger.info("Cloning %s (branch=%s) to %s", repo_url, branch, dest)
        logger.info("  (this may take several minutes for large repos...)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", "--branch", branch, "--single-branch", "--progress", clone_url, str(dest)],
            capture_output=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to clone {repo_url} (branch={branch})")

    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(dest),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    logger.info("  HEAD: %s", sha[:10])
    return sha


def step_download_spec(spec_url: str, spec_branch: str, spec_dir: Path, token: str) -> str:
    """Download/update the spec repo. Returns commit SHA."""
    _header("Step 1: Download spec repo")
    return _clone_or_pull(spec_url, spec_dir, spec_branch, token)


def step_download_dm_xmls(sdk_url: str, sdk_branch: str, sdk_dir: Path, dm_path: str, output_dm_dir: Path, token: str) -> str:
    """Download DM XMLs from the SDK repo. Returns commit SHA."""
    _header("Step 2: Download DM XMLs from SDK repo")

    sdk_sha = _clone_or_pull(sdk_url, sdk_dir, sdk_branch, token)

    # Copy DM XML files to output
    src_dm = sdk_dir / dm_path
    if not src_dm.is_dir():
        logger.error("DM XML path not found: %s", src_dm)
        return sdk_sha

    output_dm_dir.mkdir(parents=True, exist_ok=True)
    xml_count = 0
    for f in src_dm.rglob("*.xml"):
        shutil.copy2(f, output_dm_dir / f.name)
        xml_count += 1

    logger.info("Copied %d DM XML files to %s", xml_count, output_dm_dir)
    return sdk_sha


def step_download_test_plans(
    tp_url: str, tp_branch: str, tp_dir: Path, output_tp_dir: Path, token: str
) -> str:
    """Download test plans repo, build HTML, copy to output. Returns commit SHA."""
    _header("Step 3: Download test plans + build HTML")

    tp_sha = _clone_or_pull(tp_url, tp_dir, tp_branch, token)

    logger.info("Building test plan HTMLs (make html-all)...")
    t0 = time.time()
    result = subprocess.run(["make", "html-all"], cwd=str(tp_dir))
    elapsed = time.time() - t0

    if result.returncode != 0:
        logger.error("Test plan HTML build failed after %.0fs", elapsed)
        logger.info("  Falling back to raw adoc files...")
        # Copy raw adoc files as fallback
        output_tp_dir.mkdir(parents=True, exist_ok=True)
        adoc_count = 0
        for f in tp_dir.rglob("*.adoc"):
            if ".git" not in str(f):
                shutil.copy2(f, output_tp_dir / f.name)
                adoc_count += 1
        logger.info("Copied %d raw adoc files to %s", adoc_count, output_tp_dir)
        return tp_sha

    logger.info("Test plan HTML built in %.0fs", elapsed)

    # Copy generated HTML files to output
    output_tp_dir.mkdir(parents=True, exist_ok=True)
    html_count = 0
    for html_dir in [tp_dir / "build", tp_dir / "output", tp_dir / "build" / "html"]:
        if html_dir.is_dir():
            for f in html_dir.rglob("*.html"):
                shutil.copy2(f, output_tp_dir / f.name)
                html_count += 1

    if html_count == 0:
        # Try top-level HTML files (some repos output there)
        for f in tp_dir.glob("*.html"):
            shutil.copy2(f, output_tp_dir / f.name)
            html_count += 1

    logger.info("Copied %d test plan HTML files to %s", html_count, output_tp_dir)
    return tp_sha


def step_build_spec_html(spec_dir: Path) -> None:
    """Build spec HTML from the spec repo using Docker."""
    _header("Step 4: Build spec HTML (Docker)")

    # Check Docker
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        if result.returncode != 0:
            logger.warning("Docker not running — skipping HTML build. Use pre-existing spec HTMLs.")
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("Docker not found — skipping HTML build.")
        return

    logger.info("Building spec HTML via Docker (this takes 15-30 minutes)...")
    t0 = time.time()
    result = subprocess.run(
        ["make", "ENABLE_PARAGRAPH_NUMBERING=1", "html", "html-appclusters-book"],
        cwd=str(spec_dir),
    )
    elapsed = time.time() - t0

    if result.returncode != 0:
        logger.error("Spec HTML build failed after %.0fs", elapsed)
    else:
        logger.info("Spec HTML built in %.0fs", elapsed)


def step_copy_spec_html(spec_dir: Path, output_spec_dir: Path) -> None:
    """Copy built spec HTMLs to the output directory."""
    build_html = spec_dir / "build" / "html"
    if not build_html.is_dir():
        logger.warning("No build/html/ directory — spec HTML not built. Skipping copy.")
        return

    output_spec_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for f in build_html.glob("*.html"):
        if "_diff" not in f.name:
            shutil.copy2(f, output_spec_dir / f.name)
            copied += 1

    logger.info("Copied %d spec HTML files to %s", copied, output_spec_dir)


def step_build_kg_and_faiss(output_dir: Path, additional_config: str, config_path: str) -> None:
    """Build KG + FAISS index using the pipeline."""
    _header("Step 5: Build Knowledge Graph + FAISS Vector DB")

    cmd = [
        sys.executable,
        "scripts/run_ghpr_analysis.py",
        "--config", config_path,
        "--index-only",
    ]
    if additional_config:
        cmd += ["--additional-config", additional_config]

    logger.info("Running: %s", " ".join(cmd))
    t0 = time.time()
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    elapsed = time.time() - t0

    if result.returncode != 0:
        logger.error("KG/FAISS build failed after %.0fs", elapsed)
    else:
        logger.info("KG + FAISS built in %.0fs", elapsed)


def step_write_manifest(
    output_dir: Path, spec_sha: str, sdk_sha: str, spec_branch: str, sdk_branch: str,
    tp_sha: str = "", tp_branch: str = "",
) -> None:
    """Write manifest.json with build metadata."""
    manifest = {
        "built_at": datetime.now().isoformat(),
        "spec_commit": spec_sha,
        "spec_branch": spec_branch,
        "sdk_commit": sdk_sha,
        "sdk_branch": sdk_branch,
        "test_plans_commit": tp_sha,
        "test_plans_branch": tp_branch,
        "pipeline_version": "1.0",
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("Manifest written: %s", manifest_path)
    for k, v in manifest.items():
        logger.info("  %s: %s", k, v)


def step_build_docker_image(tag: str, output_dir: Path) -> int:
    """Build a minimal Docker image containing only pre-built data."""
    _header("Step 7: Build Docker image")

    # Minimal data-only image — no Python, no pip, no pipeline code.
    # The PR analysis workflow extracts data via `docker cp`.
    dockerfile_content = f"""\
FROM alpine:3.20

COPY data/knowledge_graph/ /data/knowledge_graph/
COPY data/faiss_index/     /data/faiss_index/
COPY data/data_model/      /data/data_model/
COPY data/matter_spec/     /data/matter_spec/
COPY data/test_plans/      /data/test_plans/
COPY data/manifest.json    /data/manifest.json

VOLUME ["/data"]
CMD ["echo", "matter-rag base data image — use docker cp to extract"]
"""
    dockerfile_path = PROJECT_ROOT / "Dockerfile.base"
    dockerfile_path.write_text(dockerfile_content, encoding="utf-8")

    logger.info("Building Docker image: %s", tag)
    t0 = time.time()
    result = subprocess.run(
        ["docker", "build", "-f", str(dockerfile_path), "-t", tag, "."],
        cwd=str(PROJECT_ROOT),
    )
    elapsed = time.time() - t0

    # Clean up generated Dockerfile
    dockerfile_path.unlink(missing_ok=True)

    if result.returncode != 0:
        logger.error("Docker build failed after %.0fs", elapsed)
        return result.returncode

    logger.info("Docker image built: %s (%.0fs)", tag, elapsed)
    return 0


def step_push_docker_image(image: str) -> int:
    """Push Docker image to registry."""
    _header("Step 8: Push Docker image")
    logger.info("Pushing: %s", image)

    result = subprocess.run(["docker", "push", image])
    if result.returncode != 0:
        logger.error("Docker push failed. Make sure you're logged in: docker login")
        return result.returncode

    logger.info("Pushed successfully: %s", image)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Docker image with pre-built KG, FAISS, spec HTML, and DM XMLs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", default="config/config.yaml",
        help="Base config file (default: config/config.yaml)",
    )
    parser.add_argument(
        "--additional-config", metavar="FILE", default="",
        help="Overlay config for additional sources",
    )
    parser.add_argument(
        "--spec-url", default="",
        help="Spec repo URL (default: from config.spec_repo.url)",
    )
    parser.add_argument(
        "--spec-branch", default="master",
        help="Spec repo branch (default: master)",
    )
    parser.add_argument(
        "--sdk-url", default="https://github.com/project-chip/connectedhomeip.git",
        help="SDK repo URL for DM XMLs",
    )
    parser.add_argument(
        "--sdk-branch", default="master",
        help="SDK repo branch (default: master)",
    )
    parser.add_argument(
        "--sdk-dm-path", default="data_model/clusters",
        help="Path to DM XML files within the SDK repo (default: data_model/clusters)",
    )
    parser.add_argument(
        "--test-plans-url",
        default="https://github.com/CHIP-Specifications/chip-test-plans.git",
        help="Test plans repo URL",
    )
    parser.add_argument(
        "--test-plans-branch", default="master",
        help="Test plans repo branch (default: master)",
    )
    parser.add_argument(
        "--skip-test-plans", action="store_true",
        help="Skip test plans download + build (use pre-existing test plan HTMLs)",
    )
    parser.add_argument(
        "--output", "-o", default="",
        help="Output directory for base artifact (default: data/)",
    )
    parser.add_argument(
        "--work-dir", default="",
        help="Working directory for repo clones (default: /tmp/matter-rag-base/)",
    )
    parser.add_argument(
        "--skip-spec-build", action="store_true",
        help="Skip Docker-based spec HTML build (use pre-existing HTMLs)",
    )
    parser.add_argument(
        "--tag", default="matter-rag-base:latest",
        help="Docker image tag (default: matter-rag-base:latest)",
    )
    parser.add_argument(
        "--push", metavar="REGISTRY/IMAGE:TAG", default="",
        help="Push the built image to this registry. "
             "Example: myregistry/matter-rag-base:latest or ghcr.io/org/matter-rag:latest",
    )
    parser.add_argument(
        "--no-docker", action="store_true",
        help="Build data only (KG, FAISS, DM XMLs) — skip Docker image creation",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    from src.config.config_loader import load_config
    config = load_config(args.config, additional_config=args.additional_config or None)

    token = os.environ.get("GITHUB_TOKEN", "")
    work_dir = Path(args.work_dir or "/tmp/matter-rag-base")
    output_dir = Path(args.output or str(PROJECT_ROOT / "data"))

    spec_url = args.spec_url or config.spec_repo.url
    spec_dir = work_dir / "spec-repo"
    sdk_dir = work_dir / "sdk-repo"
    tp_dir = work_dir / "test-plans-repo"

    t0 = time.time()

    # ── Step 1: Download spec repo ────────────────────────────────────────
    spec_sha = step_download_spec(spec_url, args.spec_branch, spec_dir, token)

    # ── Step 2: Download DM XMLs ──────────────────────────────────────────
    dm_output = output_dir / "data_model"
    sdk_sha = step_download_dm_xmls(args.sdk_url, args.sdk_branch, sdk_dir, args.sdk_dm_path, dm_output, token)

    # ── Step 3: Download test plans + build HTML ──────────────────────────
    tp_sha = ""
    if not args.skip_test_plans:
        tp_output = output_dir / "test_plans"
        tp_sha = step_download_test_plans(
            args.test_plans_url, args.test_plans_branch, tp_dir, tp_output, token
        )
    else:
        logger.info("Skipping test plans download (--skip-test-plans)")

    # ── Step 4: Build spec HTML ───────────────────────────────────────────
    if not args.skip_spec_build:
        step_build_spec_html(spec_dir)

    spec_html_output = output_dir / "matter_spec"
    step_copy_spec_html(spec_dir, spec_html_output)

    # ── Step 5: Build KG + FAISS ──────────────────────────────────────────
    step_build_kg_and_faiss(output_dir, args.additional_config, args.config)

    # ── Step 6: Write manifest ────────────────────────────────────────────
    _header("Step 6: Write manifest")
    step_write_manifest(
        output_dir, spec_sha, sdk_sha, args.spec_branch, args.sdk_branch,
        tp_sha=tp_sha, tp_branch=args.test_plans_branch,
    )

    # ── Step 7: Build Docker image ────────────────────────────────────────
    if not args.no_docker:
        image_tag = args.push or args.tag
        rc = step_build_docker_image(image_tag, output_dir)
        if rc != 0:
            return rc

        # ── Step 8: Push Docker image ─────────────────────────────────────
        if args.push:
            rc = step_push_docker_image(args.push)
            if rc != 0:
                return rc

    elapsed = time.time() - t0
    _header(f"Build complete ({elapsed:.0f}s)")
    print(f"  Output:     {output_dir}")
    print(f"  Spec:       {spec_sha[:10]} ({args.spec_branch})")
    print(f"  SDK:        {sdk_sha[:10]} ({args.sdk_branch})")
    if tp_sha:
        print(f"  Test Plans: {tp_sha[:10]} ({args.test_plans_branch})")
    if not args.no_docker:
        print(f"  Docker:     {args.push or args.tag}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
