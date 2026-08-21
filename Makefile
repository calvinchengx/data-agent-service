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
COMPOSE  = docker compose --env-file $(ENVFILE) $(PROFILE)

ifeq ($(OS),Windows_NT)
  SHELL := sh.exe
  .SHELLFLAGS := -c
endif

PY ?= $(shell for c in python3 python py; do if "$$c" -c '' >/dev/null 2>&1; then echo "$$c"; break; fi; done)

.PHONY: help doctor up down restart clean status logs ps pull seed test eval load ask

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

seed: ## Seed warehouse data, OpenMetadata semantics, authz, and APIM resources (Phases 1-6)
	$(PY) seed/run.py --env $(ENV)

test: ## Unit + e2e witnesses (Phase 4+)
	$(PY) e2e/run.py --env $(ENV)

eval: ## Accuracy evals per use case (Phase 7)
	$(PY) evals/runner.py --env $(ENV)

load: ## Load tests (Phase 8)
	$(PY) load/run.py --env $(ENV)

ask: ## Ask the agent a question: make ask Q="..."
	$(PY) -m agent.cli "$(Q)"
