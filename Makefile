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
# `make stack` runs the benchmark its witnesses read. Short and permissive by
# default: the point there is that both executors serve load, not what a shared
# runner's latency is. Override for a real measurement.
STACK_LOAD_ARGS ?= --vus 5 --stage 10s --p95 5000
ENVFILE := $(if $(filter prod,$(ENV)),.env.prod,.env)
COMPOSE  = ENVFILE=$(ENVFILE) docker compose --env-file $(ENVFILE) $(PROFILE)
TOOLS    = $(COMPOSE) --profile tools run --rm -e ANTHROPIC_API_KEY -e ANTHROPIC_AUTH_TOKEN tools

ifeq ($(OS),Windows_NT)
  SHELL := sh.exe
  .SHELLFLAGS := -c
endif

PY ?= $(shell for c in python3.13 python3.12 python3 python py; do if "$$c" -c 'import sys; assert sys.version_info >= (3,12)' >/dev/null 2>&1; then echo "$$c"; break; fi; done)

.PHONY: help doctor up down restart clean status logs ps pull tools-build stack seed test eval load load-compare lint format typecheck conformance conformance-one client-config ask docs coverage coverage-python coverage-go coverage-manifest release-version witnesses-manifest witnesses-check unit guard-corpus

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
	$(MAKE) load-compare ARGS="$(STACK_LOAD_ARGS)"
	$(MAKE) load ARGS="$(STACK_LOAD_ARGS)"
	$(MAKE) test

seed: ## Seed warehouse data, OpenMetadata semantics, authz, and APIM resources (Phases 1-6)
	$(TOOLS) python -m seed.run $(ARGS)

# Linting runs in containers for the same reason everything else does: a fresh
# clone of this repo needs Docker and nothing else. A host-installed linter
# moves the failure onto whoever clones. LINT_MODE=host is the escape hatch,
# not the default.
GOLANGCI  = golangci/golangci-lint:v2.13.1
# Pinned like every other image here: a toolchain that floats is a build that
# passes on a laptop and fails in CI for reasons nobody changed.
GO_IMAGE  = golang:1.26
# Terraform runs in a container like every other tool here, so a fresh clone
# still needs Docker and nothing else. `init -backend=false` downloads the
# providers and touches no state, which is what makes these checks offline.
TERRAFORM = docker run --rm -v "$(PWD)/infra/terraform:/w" -w /w hashicorp/terraform:1.14
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
	@echo "== terraform (infra)";       $(TERRAFORM) fmt -check -recursive
	@$(TERRAFORM) init -backend=false -input=false >/dev/null && $(TERRAFORM) validate
	@echo "== ty (python types)";       $(TY) check
	@echo "== annotations vs bodies";   $(TOOLS) python -m scripts.check_annotations
	@echo "== docs nav vs docs/";       python3 scripts/check_docs_nav.py
	@echo "== pull cmds vs newest tag"; python3 -m scripts.check_version
	@echo "== witness totals in prose"; $(TOOLS) python -m scripts.check_counts
	@echo "== no emulator-only paths";  sh scripts/check-discipline.sh
	@echo "== no dev-only paths";       $(TOOLS) python -m scripts.check_prod_paths --strict
	@echo "== golangci-lint (go)";      $(GOLINT)

format: ## Apply formatting and safe fixes — the only target that edits files
	$(TERRAFORM) fmt -recursive
	$(RUFF) check . --fix
	$(RUFF) format .
	docker run --rm -v "$(PWD):/src" -w /src/services/warehouse-query-go $(GOLANGCI) golangci-lint fmt ./...

guard-corpus: ## Re-record the Python guard's verdict on every contract statement
	uv run python services/contract/gen_guard_corpus.py

conformance: ## The executor contract, against BOTH implementations in turn
	@$(MAKE) --no-print-directory conformance-one DAS_EXECUTOR=py
	@$(MAKE) --no-print-directory conformance-one DAS_EXECUTOR=go

conformance-one: ## The contract against one implementation (DAS_EXECUTOR=py|go)
	# --force-recreate because `up -d --build` leaves the OLD container running
	# when only the build arg changed, and the contract would then be measured
	# against the implementation that was already up.
	DAS_EXECUTOR=$(DAS_EXECUTOR) docker compose up -d --build --force-recreate --wait warehouse-query
	# On the HOST, not in the tools container. The answer comes from `docker
	# compose ps` and an image label, and the tools container has neither the
	# docker CLI nor the socket -- asking from in there returned "unrecognised"
	# unconditionally, so the gate refused every run and named the wrong cause.
	$(PY) -m load.run --assert-executor $(DAS_EXECUTOR)
	$(TOOLS) python -m services.conformance.run --name "$(DAS_EXECUTOR) executor"

typecheck: ## Python types only
	$(TY) check

test: ## Unit + e2e witnesses (Phase 4+)
	$(TOOLS) python -m e2e.run $(ARGS)

# The floor is enforced, not reported. A coverage number nobody fails on drifts
# down one merge at a time, and the badge then tells a story the repo stopped
# living up to.
COVERAGE_FLOOR = 90

unit: ## Python unit tests alone (no stack required)
	$(TOOLS) pytest -q $(ARGS)

coverage-python: ## Python unit coverage, failing under the floor
	$(TOOLS) pytest -q --cov=agent --cov=promoter --cov=publisher --cov=services/warehouse-query-py \
		--cov-report=term-missing --cov-fail-under=$(COVERAGE_FLOOR) $(ARGS)

coverage-go: ## Go unit coverage, failing under the floor
	docker run --rm -v "$(PWD):/src" -w /src/services/warehouse-query-go $(GO_IMAGE) \
		sh -c 'go test ./... -coverprofile=/tmp/cover.out >/dev/null && \
		go tool cover -func=/tmp/cover.out | tail -1 | \
		awk -v floor=$(COVERAGE_FLOOR) "{gsub(/%/,\"\",\$$NF); \
		printf \"go coverage: %s%%\\n\", \$$NF; \
		if (\$$NF+0 < floor) { printf \"below the %s%% floor\\n\", floor; exit 1 }}"'

publisher-contract: ## The Plan contract: regenerate, diff, and hold Go to the bytes
	$(TOOLS) python publisher/contract/gen_cases.py
	@git diff --quiet -- publisher/contract/cases.json \
		&& echo "the recorded artefacts are the Python generator's" \
		|| { echo "publisher/contract/cases.json is stale — commit the regenerated file"; exit 1; }
	docker run --rm -v "$(PWD):/src" -w /src/publisher-go $(GO_IMAGE) go test ./...

coverage: coverage-python coverage-go ## Both suites, both floors

coverage-manifest: ## Record this run's coverage into docs/coverage.json (badge source)
	python3 scripts/coverage_manifest.py

GITLEAKS = docker run --rm -v "$(PWD):/repo" -w /repo zricethezav/gitleaks:v8.30.0

secrets: ## Scan the working tree AND the history for committed secrets
	@echo "== gitleaks (working tree)"; $(GITLEAKS) dir . --config .gitleaks.toml --no-banner --redact --verbose
	@echo "== gitleaks (history)";      $(GITLEAKS) git . --config .gitleaks.toml --no-banner --redact --verbose

vulns: ## Reachable vulnerabilities in the Go executor (govulncheck)
	docker run --rm -v "$(PWD):/src" -w /src/services/warehouse-query-go $(GO_IMAGE) \
		sh -c 'go run golang.org/x/vuln/cmd/govulncheck@latest ./...'

release-version: ## Set the project version, then commit and tag it (V=0.1.2)
	@test -n "$(V)" || { echo "usage: make release-version V=0.1.2"; exit 1; }
	uv version --frozen "$(V)"
	@echo
	@echo "Now, in one commit, then tag THAT commit:"
	@echo "    git commit -m 'Release v$(V)' -- pyproject.toml uv.lock"
	@echo "    git tag -a v$(V) -m 'v$(V)' && git push origin main v$(V)"
	@echo
	@echo "The release refuses to publish if the tag and pyproject disagree."

witnesses-manifest: ## Record this run's witness counts into docs/witnesses.json
	$(TOOLS) python -m e2e.run --write-manifest $(ARGS)

witnesses-check: ## Fail if docs/witnesses.json disagrees with a real run
	$(TOOLS) python -m e2e.run --check-manifest $(ARGS)

# The docs site is the one toolchain with two entry points, and the reason is
# worth stating because the obvious setups both fail.
#
# `make docs` runs pnpm on the HOST: it produces a static site rather than
# touching the stack, and a container round-trip for every edit would make
# writing docs slower than writing code.
#
# `make docs-container` needs Docker and nothing else, which is what the rest
# of this repository promises and what CI does.
#
# The trap is that node_modules holds PLATFORM-SPECIFIC binaries (esbuild), and
# a bind-mounted checkout gives host and container the same directory. Whoever
# installs last breaks the other, and the failure names esbuild rather than the
# install, so it reads as a broken dependency. It has now cost a day in each
# direction: a container install broke the host build, and later a host install
# broke the container build.
#
# So the container gets its OWN node_modules, in named volumes mounted over the
# bind mount. Both paths work, neither can overwrite the other's binaries, and
# the volumes persist so the install is paid for once. `make docs-clean` drops
# them if they ever need rebuilding.
DOCS_VOLUMES = -v das-docs-modules:/w/node_modules -v das-docs-web-modules:/w/website/node_modules
DOCS_NODE    = docker run --rm -v "$(PWD):/w" $(DOCS_VOLUMES) -w /w node:22-alpine

docs: ## Build the documentation site locally (pnpm on the host — fast for editing)
	pnpm install --frozen-lockfile
	pnpm run docs:build

docs-container: ## Build the documentation site in a container (Docker and nothing else)
	$(DOCS_NODE) sh -c "corepack enable && pnpm install --frozen-lockfile && pnpm run docs:build"

docs-clean: ## Drop the container's node_modules volumes
	-docker volume rm das-docs-modules das-docs-web-modules

eval: ## Accuracy evals per use case (Phase 7) — needs ANTHROPIC_API_KEY
	$(TOOLS) python -m evals.runner $(ARGS)

# The same questions on a Claude subscription instead of an API key. Runs on
# the HOST because that is where `claude` and its credential are; the script
# arranges the token and the source addresses that crossing implies.
eval-cli: ## Accuracy evals through the `claude` CLI (no API key needed)
	@sh scripts/eval-cli.sh $(ARGS)

load: ## Load tests (Phase 8) — k6 in a container on the stack's network
	$(PY) -m load.run $(ARGS)

# --force-recreate on every swap: `--build` alone rebuilds the image and then
# leaves the OLD container running, so the swap silently does not happen and
# the comparison measures one implementation twice.
load-compare: ## Measure BOTH executors under the same load (Phase 9; writes load-py.json and load-go.json)
	@echo "== python executor"
	$(COMPOSE) up -d --build --force-recreate --wait warehouse-query
	DAS_REPORT_STAMP=py $(PY) -m load.run --only query-direct --expect-executor py $(ARGS)
	@echo "== swapping in the go executor (DAS_EXECUTOR=go); nothing above it changes"
	DAS_EXECUTOR=go $(COMPOSE) up -d --build --force-recreate --wait warehouse-query
	DAS_REPORT_STAMP=go $(PY) -m load.run --only query-direct --expect-executor go $(ARGS)
	@echo "== restoring the python executor"
	$(COMPOSE) up -d --build --force-recreate --wait warehouse-query

client-config: ## Paste-ready MCP client configuration (ARGS="--auth token")
	$(COMPOSE) --profile tools run --rm -e DAS_HOST_REPO="$(PWD)" tools \
		python -m e2e.clients.configs $(ARGS)

ask: ## Ask the agent a question: make ask Q="..."
	$(TOOLS) python -m agent.cli $(ARGS) "$(Q)"
