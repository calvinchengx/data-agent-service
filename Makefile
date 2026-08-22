# data-agent-service — thin wrappers over docker compose, same verbs as the
# emulator family. The compose file is the source of truth.
#
#   make doctor   # toolchain + docker context
#   make up       # start the stack (entra, keyvault, arm, fabric+sqlserver, OpenMetadata, apim)
#   make status   # is it usable? (non-zero if not)
#   make down     # stop; make clean also drops volumes
#
# ENV=prod points every harness at real Azure via .env.prod (discipline rule 2).
ENV     ?= local
ENVFILE := $(if $(filter prod,$(ENV)),.env.prod,.env)
COMPOSE  = ENVFILE=$(ENVFILE) docker compose --env-file $(ENVFILE) $(PROFILE)
TOOLS    = $(COMPOSE) --profile tools run --rm -e ANTHROPIC_API_KEY -e ANTHROPIC_AUTH_TOKEN tools

ifeq ($(OS),Windows_NT)
  SHELL := sh.exe
  .SHELLFLAGS := -c
endif

PY ?= $(shell for c in python3.13 python3.12 python3 python py; do if "$$c" -c 'import sys; assert sys.version_info >= (3,12)' >/dev/null 2>&1; then echo "$$c"; break; fi; done)

.PHONY: help doctor up down restart clean status logs ps pull tools-build stack seed test eval load lint format typecheck conformance client-config ask

help: ## Show the available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  %-10s %s\n", $$1, $$2}'

doctor: ## Check the toolchain and the docker context
	@sh scripts/doctor.sh

pull: ## Pull the pinned dependency images
	$(COMPOSE) pull

up: ## Start the stack in the background
	@test -f $(ENVFILE) || cp .env.example $(ENVFILE)
	$(COMPOSE) up -d

down: ## Stop and remove containers
	$(COMPOSE) down

clean: ## Stop and remove containers AND volumes (full reset)
	$(COMPOSE) down -v

restart: clean up ## Full reset, then start again

status: ## Report whether the stack is usable (non-zero exit if not)
	@sh scripts/status.sh

ps: ## Container states
	$(COMPOSE) ps

logs: ## Follow logs (SERVICE=name to filter)
	$(COMPOSE) logs -f $(SERVICE)

tools-build: ## Build the tools image (seeds/harnesses runtime)
	$(COMPOSE) --profile tools build tools

stack: ## Everything from nothing: start, seed, apply, verify (what CI runs)
	$(MAKE) up
	$(MAKE) seed
	@echo "== applying ids the seed created (the executor reads them at start)"
	$(COMPOSE) up -d
	$(MAKE) test

seed: ## Seed warehouse data, OpenMetadata semantics, authz, and APIM resources (Phases 1-6)
	$(TOOLS) python -m seed.run $(ARGS)

# Linting runs in containers for the same reason everything else does: a fresh
# clone of this repo needs Docker and nothing else. A host-installed linter
# moves the failure onto whoever clones. LINT_MODE=host is the escape hatch,
# not the default.
GOLANGCI  = golangci/golangci-lint:v2.6.1
LINT_MODE ?= container
ifeq ($(LINT_MODE),host)
  RUFF = uv run ruff
  TY   = uv run ty
  GOLINT = golangci-lint run ./...
else
  RUFF = $(TOOLS) ruff
  TY   = $(TOOLS) ty
  GOLINT = docker run --rm -v "$(PWD):/src" -w /src/services/warehouse-query-go $(GOLANGCI) golangci-lint run ./...
endif

lint: ## Lint and type-check everything (never edits; use `make format` for that)
	@echo "== ruff (python lint)";      $(RUFF) check .
	@echo "== ruff (python format)";    $(RUFF) format --check .
	@echo "== ty (python types)";       $(TY) check
	@echo "== golangci-lint (go)";      $(GOLINT)

format: ## Apply formatting and safe fixes — the only target that edits files
	$(RUFF) check . --fix
	$(RUFF) format .
	docker run --rm -v "$(PWD):/src" -w /src/services/warehouse-query-go $(GOLANGCI) golangci-lint fmt ./...

conformance: ## The executor contract, against whichever implementation is running
	$(TOOLS) python -m services.conformance.run

typecheck: ## Python types only
	$(TY) check

test: ## Unit + e2e witnesses (Phase 4+)
	$(TOOLS) python -m e2e.run $(ARGS)

eval: ## Accuracy evals per use case (Phase 7)
	$(TOOLS) python -m evals.runner $(ARGS)

load: ## Load tests (Phase 8) — k6 in a container on the stack's network
	$(PY) -m load.run $(ARGS)

client-config: ## Paste-ready MCP client configuration (ARGS="--auth token")
	$(TOOLS) python -m e2e.clients.configs $(ARGS)

ask: ## Ask the agent a question: make ask Q="..."
	$(TOOLS) python -m agent.cli $(ARGS) "$(Q)"
