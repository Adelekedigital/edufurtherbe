.DEFAULT_GOAL := help
.PHONY: help install run test check build hooks clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

install: ## Install dependencies and the git hooks
	uv sync --all-extras --dev
	uv run pre-commit install

run: ## Start the API with reload
	uv run uvicorn app.main:app --reload --app-dir src

test: ## Run the full test suite
	uv run pytest

check: ## The full local gate — run before every commit
	uv run python scripts/check.py

build: ## Build the distribution
	uv build

hooks: ## Run every pre-commit hook against all files
	uv run pre-commit run --all-files

clean: ## Remove build and cache artefacts
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml
