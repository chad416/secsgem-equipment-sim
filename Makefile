# gemsim - SECS/GEM equipment simulator
# GNU make on Windows (cmd/PowerShell) or Linux/macOS. Without make, run the
# commands below directly (see README, "Run it").

PYTHON ?= python
ifeq ($(OS),Windows_NT)
  VENV_PY := .venv/Scripts/python.exe
else
  VENV_PY := .venv/bin/python
endif
export PYTHONPATH := src

.PHONY: help install demo test lint format typecheck check coverage evidence run-controller run-equipment run-host

help:
	@echo make install         create .venv, install pinned dev dependencies and gemsim (editable)
	@echo make demo            controller + GEM equipment + host lot run with fault and recovery, output in out/
	@echo make check           ruff lint + format check, mypy --strict, pytest
	@echo make coverage        regenerate docs/message_coverage.md from the dispatch table
	@echo make evidence        refresh the committed run in artifacts/ (demo + check output)

$(VENV_PY):
	$(PYTHON) -m venv .venv

install: $(VENV_PY)
	$(VENV_PY) -m pip install -r requirements-dev.txt
	$(VENV_PY) -m pip install --no-deps -e .

demo:
	$(VENV_PY) -m gemsim demo

test:
	$(VENV_PY) -m pytest -q

lint:
	$(VENV_PY) -m ruff check src tests scripts
	$(VENV_PY) -m ruff format --check src tests scripts

format:
	$(VENV_PY) -m ruff format src tests scripts

typecheck:
	$(VENV_PY) -m mypy

check: lint typecheck test

coverage:
	$(VENV_PY) -m gemsim coverage --write docs/message_coverage.md

evidence:
	$(VENV_PY) -m gemsim demo --out artifacts > artifacts/demo_console.txt 2>&1
	$(MAKE) --no-print-directory check > artifacts/check_output.txt 2>&1

# Three-process mode (three terminals), the way the parts run on a real tool:
run-controller:
	$(VENV_PY) -m gemsim controller --time-scale 20

run-equipment:
	$(VENV_PY) -m gemsim equipment --online --remote

run-host:
	$(VENV_PY) -m gemsim host --scenario lot --trace out/host_trace.sml
