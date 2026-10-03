# Backend + infra targets. `make help` lists them.
SHELL       := /bin/bash
API_DIR     := backend/api
PY          ?= /home/linuxbrew/.linuxbrew/bin/python3.13
VENV        := $(API_DIR)/.venv
VPY         := $(VENV)/bin/python
COMPOSE     := docker compose -f infra/docker-compose.yml
BASE_URL    ?= http://localhost:8000
LB_URL      ?= https://localhost
N           ?= 3
PSQL        ?= psql

.PHONY: help venv api-dev api-test contract-check worker up down logs scale lb-demo lb-native certs db-migrate

help: ## list targets
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

$(VENV)/.installed: $(API_DIR)/pyproject.toml
	@test -x $(VPY) || $(PY) -m venv $(VENV)
	$(VPY) -m pip install -q -e '$(API_DIR)[dev]'
	@touch $@

venv: $(VENV)/.installed ## create backend/api/.venv with deps

api-dev: venv ## API on :8000, fixtures mode, hot reload (override DATA_MODE=db DATABASE_URL=...)
	cd $(API_DIR) && DATA_MODE=$${DATA_MODE:-fixtures} INSTANCE_ID=$${INSTANCE_ID:-dev} \
	  .venv/bin/uvicorn atlas_api.main:app --reload --port 8000

api-test: venv ## pytest for the API (fixtures mode)
	cd $(API_DIR) && .venv/bin/python -m pytest -q

contract-check: ## validate a running API: make contract-check BASE_URL=... [ARGS=--allow-missing]
	python3 backend/scripts/check_contract.py --base-url $(BASE_URL) $(ARGS)

worker: venv ## run the job worker locally (needs REDIS_URL)
	cd $(API_DIR) && .venv/bin/python -m atlas_api.worker

certs: ## self-signed dev TLS cert for the L7 tier
	infra/scripts/gen-dev-cert.sh

up: certs ## start the full LB stack (docker compose)
	$(COMPOSE) up -d --build

down: ## stop the stack
	$(COMPOSE) down

logs: ## follow api/worker logs
	$(COMPOSE) logs -f api worker

scale: ## scale API replicas: make scale N=5
	$(COMPOSE) up -d --no-recreate --scale api=$(N)

lb-demo: ## curl the LB 10x and print which replica / L7 answered
	@for i in $$(seq 10); do \
	  curl -sk -o /dev/null -D - $(LB_URL)/api/v1/meta | tr -d '\r' | \
	  awk -F': ' 'tolower($$1)=="x-served-by"{a=$$2} tolower($$1)=="x-lb-l7"{l=$$2} END{printf "api=%-14s l7=%s\n", a, l}'; \
	done

lb-native: venv ## no-Docker demo: 3 local replicas + native HAProxy round robin + drain
	infra/scripts/lb-native.sh

db-migrate: ## apply backend/db/migrations in order: make db-migrate DATABASE_URL=...
	@test -n "$(DATABASE_URL)" || { echo "DATABASE_URL is required"; exit 1; }
	@for f in $$(ls backend/db/migrations/*.sql | sort); do \
	  echo "-> $$f"; $(PSQL) "$(DATABASE_URL)" -v ON_ERROR_STOP=1 -q -f "$$f" || exit 1; \
	done
