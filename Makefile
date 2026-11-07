.PHONY: help install lint format test cov docker-build up down clean

PYTHON ?= python3.12
VENV ?= .venv
BIN := $(VENV)/bin

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

install:  ## Create a virtualenv and install the package with dev extras
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -e ".[dev]"

lint:  ## Run ruff lint and format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:  ## Auto-fix lint issues and format
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

test:  ## Run the test suite
	$(BIN)/pytest

cov:  ## Run tests with coverage report
	$(BIN)/pytest --cov --cov-report=term-missing

docker-build:  ## Build the relay image
	docker build -t mytunnel-relay:local .

up:  ## Start the relay with docker compose (reads .env)
	docker compose up -d --build

down:  ## Stop the compose stack
	docker compose down

clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
