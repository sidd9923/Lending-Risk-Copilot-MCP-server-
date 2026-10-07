.PHONY: install test lint fmt smoke verify run

install:
	pip install -e ".[dev]"

test:
	pytest -q

lint:
	ruff check . && ruff format --check .

fmt:
	ruff format . && ruff check --fix .

smoke:          ## hits the real APIs; needs .env
	set -a; . ./.env; set +a; python scripts/smoke_live.py "$(or $(LENDER),Wells Fargo)"

verify:         ## checks registry pins against live sources
	set -a; . ./.env; set +a; python scripts/verify_registry.py

run:
	lending-risk-mcp
