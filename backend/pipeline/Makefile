# Rare Disease Atlas — data pipeline. Run from backend/pipeline.  `make help`
PY      ?= /home/linuxbrew/.linuxbrew/bin/python3.13
VENV    := .venv
CLI     := $(VENV)/bin/atlas-pipeline

.PHONY: help venv ingest extract reconcile analytics views load dry-run snapshot all
help: ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-11s %s\n", $$1, $$2}'

venv: $(VENV)/.installed ## create .venv with deps (incl. igraph + leidenalg)
$(VENV)/.installed: pyproject.toml
	$(PY) -m venv $(VENV) && $(VENV)/bin/pip install -q -e '.[graph]' && touch $@

ingest: venv ## download + parse all sources (cached in data/raw)
	$(CLI) ingest
extract: venv ## OpenAI claim extraction over abstracts (add ARGS=--limit 50 to test)
	$(CLI) extract $(ARGS)
reconcile: venv ## map extracted names to ids -> literature edges
	$(CLI) reconcile
analytics: venv ## mechanisms, IC, similarity, counterexamples, Leiden, overlap, paths
	$(CLI) analytics
views: venv ## ActionView / MechanismView payloads
	$(CLI) views
dry-run: venv ## validate everything without writing
	$(CLI) load --dry-run
load: venv ## load into Postgres (needs DATABASE_URL)
	$(CLI) load
snapshot: venv ## real API responses -> data/snapshot (needs ATLAS_API_BASE)
	$(CLI) export-fixtures
all: ingest extract reconcile analytics views load ## full rebuild
