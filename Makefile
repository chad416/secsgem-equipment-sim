# gemsim - SECS/GEM equipment simulator
# Works with GNU make on Windows (cmd/PowerShell) and Linux/macOS.

PYTHON ?= python
ifeq ($(OS),Windows_NT)
  VENV_PY := .venv/Scripts/python.exe
else
  VENV_PY := .venv/bin/python
endif
export PYTHONPATH := src

.PHONY: help install demo demo-quiet test lint format typecheck check run-controller run-equipment run-host

help:
	@echo make install         create .venv and install pinned dev dependencies
	@echo make demo            controller + GEM equipment + host lot run with fault and recovery
	@echo make test            pytest (unit + end-to-end)
	@echo make lint            ruff lint + format check
	@echo make typecheck       mypy --strict
	@echo make check           lint + typecheck + test

$(VENV_PY):
	$(PYTHON) -m venv .venv

install: $(VENV_PY)
	$(VENV_PY) -m pip install -r requirements-dev.txt

demo:
	$(VENV_PY) -m gemsim demo

demo-quiet:
	$(VENV_PY) -m gemsim demo --quiet

test:
	$(VENV_PY) -m pytest -q

lint:
	$(VENV_PY) -m ruff check src tests
	$(VENV_PY) -m ruff format --check src tests

format:
	$(VENV_PY) -m ruff format src tests

typecheck:
	$(VENV_PY) -m mypy

check: lint typecheck test

# Three-process mode (three terminals), the way the parts would run on real equipment:
run-controller:
	$(VENV_PY) -m gemsim controller --time-scale 20

run-equipment:
	$(VENV_PY) -m gemsim equipment --online --remote

run-host:
	$(VENV_PY) -m gemsim host --scenario lot --trace artifacts/host_trace.sml
