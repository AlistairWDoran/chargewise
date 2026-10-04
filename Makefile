.PHONY: dev test lint typecheck fmt ingest-sample

dev:            ## Run backend + frontend with sample data
	docker compose up --build

test:           ## Run the backend test suite
	cd backend && python -m pytest

lint:           ## Lint (ruff) the backend
	cd backend && ruff check .

typecheck:      ## Static type-check the backend
	cd backend && mypy chargewise

fmt:            ## Auto-format the backend
	cd backend && ruff format .

ingest-sample:  ## Load bundled sample fixtures into a local DB
	cd backend && python -m chargewise.ingest.sample
