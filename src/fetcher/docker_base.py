"""Docker Base Extractor — pulls a pre-built Docker image and extracts data.

Extracts KG, FAISS index, DM XMLs, spec HTML, and manifest from a Docker
image to the local data/ directory so the pipeline can use pre-built data
without rebuilding.

Usage from code::

    from src.fetcher.docker_base import extract_docker_base
    manifest = extract_docker_base("myregistry/matter-rag-base:latest", Path("data"))
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def extract_docker_base(image: str, data_dir: Path) -> Optional[Dict]:
    """Pull a Docker image and extract pre-built pipeline data.

    Args:
        image: Docker image reference (e.g., "myregistry/matter-rag-base:latest")
        data_dir: Local directory to extract data into (e.g., "data/")

    Returns:
        Manifest dict if successful, None on failure.
    """
    # Pull the image
    logger.info("[docker_base] Pulling image: %s", image)
    result = subprocess.run(["docker", "pull", image], capture_output=True)
    if result.returncode != 0:
        logger.error("[docker_base] Failed to pull image: %s", result.stderr.decode()[:200])
        return None

    # Create a temporary container to copy data from
    logger.info("[docker_base] Extracting data from image...")
    container_id = subprocess.run(
        ["docker", "create", image],
        capture_output=True, text=True,
    ).stdout.strip()

    if not container_id:
        logger.error("[docker_base] Failed to create container from image")
        return None

    try:
        data_dir.mkdir(parents=True, exist_ok=True)

        # Extract each data directory from the container
        _PATHS = [
            "/data/knowledge_graph",
            "/data/faiss_index",
            "/data/data_model",
            "/data/matter_spec",
            "/data/test_plans",
            "/data/manifest.json",
        ]

        for src_path in _PATHS:
            dest = data_dir / src_path.split("/data/")[-1]
            try:
                # Remove existing destination to prevent docker cp nesting
                # (docker cp copies INTO existing dirs, creating dir/dir/)
                if dest.is_dir():
                    import shutil
                    shutil.rmtree(dest)
                elif dest.is_file():
                    dest.unlink()

                subprocess.run(
                    ["docker", "cp", f"{container_id}:{src_path}", str(dest)],
                    check=True, capture_output=True,
                )
                logger.info("[docker_base]   Extracted: %s", dest.name)
            except subprocess.CalledProcessError:
                logger.debug("[docker_base]   Not found in image: %s", src_path)

    finally:
        subprocess.run(["docker", "rm", container_id], capture_output=True)

    # Read manifest
    manifest_path = data_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            logger.info("[docker_base] Manifest loaded: spec=%s sdk=%s",
                        manifest.get("spec_commit", "")[:10],
                        manifest.get("sdk_commit", "")[:10])
            return manifest
        except Exception as exc:
            logger.warning("[docker_base] Could not read manifest: %s", exc)

    logger.info("[docker_base] Data extracted to %s (no manifest found)", data_dir)
    return {}
