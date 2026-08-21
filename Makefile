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

.PHONY: help doctor up down restart clean status logs ps pull tools-build seed test eval load ask

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

seed: ## Seed warehouse data, OpenMetadata semantics, authz, and APIM resources (Phases 1-6)
	$(TOOLS) python -m seed.run $(ARGS)

test: ## Unit + e2e witnesses (Phase 4+)
	$(TOOLS) python -m e2e.run $(ARGS)

eval: ## Accuracy evals per use case (Phase 7)
	$(TOOLS) python -m evals.runner $(ARGS)

load: ## Load tests (Phase 8) — k6 in a container on the stack's network
	$(PY) -m load.run $(ARGS)

ask: ## Ask the agent a question: make ask Q="..."
	$(TOOLS) python -m agent.cli $(ARGS) "$(Q)"
