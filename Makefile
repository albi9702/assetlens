.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test test-cov check init-db sync api db-shell clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install all dependencies
	uv sync --all-groups

lint: ## Run the linter
	uv run ruff check src tests

format: ## Format the code
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck: ## Run the type checker
	uv run mypy src

test: ## Run the test suite
	uv run pytest

test-cov: ## Run the test suite with coverage
	uv run pytest --cov --cov-report=term-missing

check: lint typecheck test ## Run every quality gate

init-db: ## Create or migrate the SQLite database
	uv run assetlens init-db

sync: ## Incremental sync of every instrument in the watchlist
	uv run assetlens sync

api: ## Start the read-only API with autoreload
	uv run assetlens api --reload

db-shell: ## Open the SQLite database in the sqlite3 shell
	sqlite3 data/assetlens.db

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
