# One definition, two callers: a developer and CI invoke exactly these targets.
# CI never hand-copies a command; if a gate changes, it changes here.
SHELL := /bin/bash
.PHONY: setup lint typecheck test test-integration check \
        image image-store image-api image-git up down logs smoke failure \
        scan scan-deps scan-secrets scan-fs scan-image \
        compose-check prose-check db-up db-down clean

SERVICES ?= store api git
COMPOSE := docker compose
COMPOSE_CI := docker compose -f docker-compose.yml -f docker-compose.ci.yml

setup:
	uv sync --all-packages

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy coppermind services tests

# Unit tests. No database, no containers, no network.
test:
	uv run pytest -q

# PostgreSQL backed tests. `make db-up` starts a throwaway server on 5433 so
# this never touches a running stack's database.
test-integration:
	COPPERMIND_TEST_DATABASE_URL=$${COPPERMIND_TEST_DATABASE_URL:-postgresql://coppermind@127.0.0.1:5433/coppermind_test} \
		uv run pytest -q tests/integration

db-up:
	docker run -d --rm --name coppermind-test-db \
		-e POSTGRES_USER=coppermind -e POSTGRES_PASSWORD=coppermind -e POSTGRES_DB=coppermind_test \
		-p 127.0.0.1:5433:5432 postgres:16 >/dev/null
	@for i in $$(seq 1 30); do \
		docker exec coppermind-test-db pg_isready -U coppermind -d coppermind_test >/dev/null 2>&1 && exit 0; \
		sleep 1; \
	done; echo "test database never became ready"; exit 1

db-down:
	-docker rm -f coppermind-test-db >/dev/null 2>&1

# Everything a pull request must pass before an image is built.
check: lint typecheck test compose-check prose-check

# `docker compose config` parses and validates both files, which catches a
# broken quickstart before anyone tries to run it.
compose-check:
	$(COMPOSE) config >/dev/null
	$(COMPOSE_CI) config >/dev/null

# House rule, enforced rather than remembered: no em-dashes anywhere in the
# tree. The lock file and this rule's own definition are excluded.
prose-check:
	bash ci/prose-check.sh

image: $(addprefix image-,$(SERVICES))

image-store:
	docker build -f services/store/Dockerfile \
		--build-arg BUILD_VERSION=$${BUILD_VERSION:-dev} \
		--build-arg BUILD_SHA=$$(git rev-parse HEAD 2>/dev/null || echo unknown) \
		--build-arg BUILD_DATE=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
		-t coppermind/store:local .

image-api:
	docker build -f services/api/Dockerfile \
		--build-arg BUILD_VERSION=$${BUILD_VERSION:-dev} \
		--build-arg BUILD_SHA=$$(git rev-parse HEAD 2>/dev/null || echo unknown) \
		--build-arg BUILD_DATE=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
		-t coppermind/api:local .

image-git:
	docker build -f services/git/Dockerfile \
		--build-arg BUILD_VERSION=$${BUILD_VERSION:-dev} \
		--build-arg BUILD_SHA=$$(git rev-parse HEAD 2>/dev/null || echo unknown) \
		--build-arg BUILD_DATE=$$(date -u +%Y-%m-%dT%H:%M:%SZ) \
		-t coppermind/git:local .

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

# The compose storyline. COMPOSE_FILES lets CI point it at the tested images.
smoke:
	COMPOSE_FILES="$${COMPOSE_FILES:--f docker-compose.yml}" bash ci/smoke.sh

# Helpers stopped and started with edits in between; takes a few minutes.
failure:
	COMPOSE_FILES="$${COMPOSE_FILES:--f docker-compose.yml}" bash ci/failure.sh

clean: down db-down
	-$(COMPOSE) down -v

# ---- security scans ---------------------------------------------------------
# Fast path: trivy and gitleaks on PATH. Fallback: the pinned scanner
# containers, run as the calling user. Both read the same committed config, so
# a developer sees what CI sees.
TRIVY_VERSION ?= 0.74.0
GITLEAKS_VERSION ?= v8.30.1
TRIVY_CACHE ?= $(HOME)/.cache/trivy
DOCKER_SOCK ?= /var/run/docker.sock
DOCKER_SOCK_GID := $(shell stat -c %g $(DOCKER_SOCK) 2>/dev/null || echo 0)
# In a git worktree .git is a file pointing outside the checkout; mount the
# common dir read-only so gitleaks can read history from inside the container.
GIT_COMMON := $(shell git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
GIT_COMMON_MOUNT := $(if $(filter $(CURDIR)/%,$(GIT_COMMON)),,$(if $(GIT_COMMON),-v "$(GIT_COMMON):$(GIT_COMMON):ro",))

ifneq ($(shell command -v trivy 2>/dev/null),)
TRIVY = trivy --cache-dir "$(TRIVY_CACHE)"
else
TRIVY = mkdir -p "$(TRIVY_CACHE)" && docker run --rm \
	--user $(shell id -u):$(shell id -g) --group-add $(DOCKER_SOCK_GID) \
	-v "$(CURDIR):/repo:ro" -w /repo \
	-v "$(TRIVY_CACHE):/cache" \
	-v "$(DOCKER_SOCK):/var/run/docker.sock" \
	aquasec/trivy:$(TRIVY_VERSION) --cache-dir /cache
endif

ifneq ($(shell command -v gitleaks 2>/dev/null),)
GITLEAKS = gitleaks
else
GITLEAKS = docker run --rm --user $(shell id -u):$(shell id -g) \
	-v "$(CURDIR):/repo" $(GIT_COMMON_MOUNT) -w /repo ghcr.io/gitleaks/gitleaks:$(GITLEAKS_VERSION)
endif

scan: scan-deps scan-secrets scan-fs

# Known-vulnerable dependencies across the whole workspace.
scan-deps:
	uv run pip-audit --skip-editable --progress-spinner off

# Committed secrets, full git history (CI checks out with fetch-depth 0).
# gitleaks exits 0 when git itself fails and it scanned nothing, so the gate
# also requires that at least one commit was actually scanned.
scan-secrets:
	log=$$(mktemp); trap 'rm -f "$$log"' EXIT; \
	$(GITLEAKS) detect --source . --no-banner --redact >"$$log" 2>&1; rc=$$?; cat "$$log"; \
	test $$rc -eq 0 && grep -q -E '[1-9][0-9]* commits scanned' "$$log"

# Repository scan: misconfiguration (Dockerfiles, compose, workflows) and known
# vulnerabilities in lockfiles. Severity and skips live in trivy.yaml.
scan-fs:
	$(TRIVY) fs --scanners vuln,misconfig .

# Built image scan. Usage: make scan-image IMAGE=coppermind/store:local
scan-image:
	@test -n "$(IMAGE)" || { echo "usage: make scan-image IMAGE=<tag>"; exit 2; }
	$(TRIVY) image $(IMAGE)
