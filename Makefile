# Break Point developer shortcuts. Every target assumes the repo root as cwd.
#
#   make venv      create .venv and install break_point in editable mode
#   make up        start the selected target's stack (BREAK_POINT_TARGET) and wait until it is healthy
#   make demo      topology -> baseline -> run -> risk -> fix -> scorecard -> narrate
#
# Override the experiment count with N=5, the LLM with LLM=--no-llm, etc.

VENV      ?= .venv
PY        ?= $(VENV)/bin/python
BREAK_POINT ?= $(VENV)/bin/break_point
N         ?= 3
LLM       ?=
PORT      ?= 8765

.PHONY: venv up down status target setup topology baseline run demo dashboard guardian test clean-reports

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

target:
	$(BREAK_POINT) target

setup:
	$(BREAK_POINT) setup

topology:
	$(BREAK_POINT) topology

baseline:
	$(BREAK_POINT) baseline

run:
	$(BREAK_POINT) run --experiments $(N) $(LLM)

demo:
	$(BREAK_POINT) demo --experiments $(N) $(LLM)

dashboard:
	$(BREAK_POINT) dashboard --port $(PORT)

guardian:
	$(BREAK_POINT) guardian

test:
	$(PY) -m pytest -q

clean-reports:
	find reports -mindepth 1 ! -name .gitkeep -exec rm -rf {} +
	@echo "reports/ cleared (kept .gitkeep)"
