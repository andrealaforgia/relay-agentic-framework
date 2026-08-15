# Relay — development and installation tasks.
#
# `make install` is the whole story: Redis, the dev environment, and `relay`
# on your PATH pointing at THIS checkout. The editable install is deliberate:
# a swarm must run the code you are working on, and a packaged install cannot
# currently find contract/, roles/ and policies/ (they are not in the wheel).

UV ?= uv
VENV := .venv
PY := $(VENV)/bin/python

.DEFAULT_GOAL := help
.PHONY: help install redis deps tool uninstall test typecheck check contract doctor clean

help:  ## Show the targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-10s\033[0m %s\n", $$1, $$2}'

install: redis deps tool  ## Everything: redis, dev env, `relay` on your PATH
	@echo
	@echo "relay is installed from $(CURDIR) — edits here take effect immediately."
	@relay --help >/dev/null 2>&1 && echo "run 'relay up <project>' to start a swarm" \
		|| echo "!! 'relay' is not on your PATH — add ~/.local/bin to it"

redis:  ## Install redis-server if it is missing (the swarm's bus and ledger)
	@if command -v redis-server >/dev/null 2>&1; then \
		echo "✓ redis-server $$(redis-server --version | awk '{print $$3}')"; \
	elif command -v brew >/dev/null 2>&1; then \
		echo "installing redis via homebrew…"; brew install redis; \
	else \
		echo "✗ redis-server is missing and homebrew is not installed."; \
		echo "  Install Redis yourself, then re-run: make install"; \
		exit 1; \
	fi

deps:  ## Create/refresh the dev environment (.venv, with test and type tooling)
	@command -v $(UV) >/dev/null 2>&1 || { \
		echo "✗ uv is not installed — https://docs.astral.sh/uv/getting-started/"; exit 1; }
	$(UV) sync

tool:  ## Put relay, relay-send, relay-id, relay-inbox on your PATH (editable)
	$(UV) tool install --editable . --force
	@echo "✓ relay → $(CURDIR)"

uninstall:  ## Remove the CLI from your PATH (the checkout and its .venv stay)
	-$(UV) tool uninstall relay-agentic-framework

test:  ## Run the suite (no model calls, no network)
	$(PY) -m pytest

typecheck:  ## mypy, strict
	$(PY) -m mypy src

check: test typecheck  ## What CI runs

contract:  ## Regenerate contract/schema/*.json and docs/PROTOCOL.md
	$(PY) -m relay.cli.main contract gen

doctor:  ## Preflight the local setup for a swarm
	$(PY) -m relay.cli.main doctor

clean:  ## Remove caches and build artefacts (never touches ledgers or ~/.relay)
	rm -rf .pytest_cache .mypy_cache dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
