# Faultline developer shortcuts. Every target assumes the repo root as cwd.
#
#   make venv      create .venv and install faultline in editable mode
#   make up        start the Ditto + monitoring sandbox and wait for /health = UP
#   make demo      topology -> baseline -> run -> risk -> fix -> scorecard -> narrate
#
# Override the experiment count with N=5, the LLM with LLM=--no-llm, etc.

VENV      ?= .venv
PY        ?= $(VENV)/bin/python
FAULTLINE ?= $(VENV)/bin/faultline
N         ?= 3
LLM       ?=
PORT      ?= 8765

.PHONY: venv up down status setup topology baseline run demo dashboard guardian test clean-reports

venv:
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"
	@echo; echo "Activate with: . $(VENV)/bin/activate"

up:
	scripts/up.sh

down:
	scripts/down.sh

status:
	scripts/status.sh

setup:
	$(FAULTLINE) setup

topology:
	$(FAULTLINE) topology

baseline:
	$(FAULTLINE) baseline

run:
	$(FAULTLINE) run --experiments $(N) $(LLM)

demo:
	$(FAULTLINE) demo --experiments $(N) $(LLM)

dashboard:
	$(FAULTLINE) dashboard --port $(PORT)

guardian:
	$(FAULTLINE) guardian

test:
	$(PY) -m pytest -q

clean-reports:
	find reports -mindepth 1 ! -name .gitkeep -exec rm -rf {} +
	@echo "reports/ cleared (kept .gitkeep)"
