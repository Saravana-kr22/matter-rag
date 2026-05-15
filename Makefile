# Matter RAG Pipeline — Makefile
#
# Convenience targets for Docker-based pipeline execution.
# First run (~20 min) builds KG + FAISS; subsequent runs (~2-5 min) use cached data.

.DEFAULT_GOAL := help
DOCKER_IMAGE  ?= matter-rag:latest
REGISTRY      ?= matter-rag

# ── Build ────────────────────────────────────────────────────────────────

.PHONY: build
build: ## Build the Docker image locally
	docker compose build

.PHONY: build-push
build-push: ## Build the Docker image and push to registry
	python scripts/helper_scripts/build_docker_image.py \
		--push $(REGISTRY):latest

.PHONY: build-base
build-base: ## Build base data only (KG + FAISS, no Docker image)
	python scripts/helper_scripts/build_docker_image.py --no-docker

# ── Run ──────────────────────────────────────────────────────────────────

.PHONY: extract-base
extract-base: ## Extract pre-built data from a base image (set BASE_IMAGE)
	@test -n "$(BASE_IMAGE)" || (echo "ERROR: Set BASE_IMAGE=registry/image:tag"; exit 1)
	python -c "\
	from src.fetcher.docker_base import extract_docker_base; \
	from pathlib import Path; \
	extract_docker_base('$(BASE_IMAGE)', Path('data'))"

.PHONY: run
run: ## Run the pipeline (requires diff HTMLs in data/input_doc/)
	docker compose run --rm pipeline

.PHONY: run-pr
run-pr: ## Analyze a spec PR (requires PR_URL and GITHUB_TOKEN env vars)
	@test -n "$(PR_URL)" || (echo "ERROR: Set PR_URL=https://github.com/.../pull/N"; exit 1)
	docker compose run --rm -e PR_URL=$(PR_URL) pipeline

.PHONY: run-cluster
run-cluster: ## Analyze a single cluster (requires CLUSTER env var)
	@test -n "$(CLUSTER)" || (echo "ERROR: Set CLUSTER='On/Off'"; exit 1)
	docker compose run --rm -e CLUSTER="$(CLUSTER)" pipeline

# ── Maintenance ──────────────────────────────────────────────────────────

.PHONY: rebuild-index
rebuild-index: ## Force rebuild KG + FAISS (ignores cached data)
	docker compose run --rm -e FORCE_REBUILD=1 pipeline

.PHONY: shell
shell: ## Drop into container bash for debugging
	docker compose run --rm --entrypoint bash pipeline

.PHONY: clean
clean: ## Remove generated artifacts (preserves source data)
	rm -rf reports/* logs/*
	rm -f data/input_doc/*_diff.html

.PHONY: clean-all
clean-all: clean ## Remove all cached data (KG, FAISS, models)
	rm -rf data/faiss_index/* data/knowledge_graph/* data/cache/*

# ── Local (no Docker) ───────────────────────────────────────────────────

.PHONY: local-run
local-run: ## Run pipeline locally (no Docker)
	python scripts/run_ghpr_analysis.py --compare-only \
		--input-doc-dir data/input_doc/ --auto-detect-clusters

.PHONY: local-pr
local-pr: ## Analyze a spec PR locally (no Docker)
	@test -n "$(PR_URL)" || (echo "ERROR: Set PR_URL=https://github.com/.../pull/N"; exit 1)
	python scripts/run_ghpr_analysis.py \
		--pr-url $(PR_URL) $(if $(SPEC_REPO),--spec-repo $(SPEC_REPO))

.PHONY: local-pr-with-base
local-pr-with-base: extract-base local-pr ## Extract base data, then analyze a PR locally

.PHONY: local-build
local-build: ## Build KG + FAISS locally
	python scripts/run_ghpr_analysis.py --index-only

# ── Help ─────────────────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
