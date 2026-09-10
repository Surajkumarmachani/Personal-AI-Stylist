.PHONY: up down logs migrate test lint fmt typecheck taxonomy reset-db check

COMPOSE := docker compose -f infra/compose/docker-compose.yml
export PYTHONPATH := packages:services

models:            ## fetch model weights into ./models (once, ~176MB)
	python scripts/download_models.py

up: models         ## start the local stack
	$(COMPOSE) up -d --build --wait
	@echo "api      http://localhost:8080/docs"
	@echo "ml       http://localhost:8081/readyz"
	@echo "minio    http://localhost:9001 (minioadmin/minioadmin)"
	@echo "web      cd web && npm install && npm run dev  -> http://localhost:3100"

verify:            ## run the exit-criteria scripts against a live stack
	python scripts/verify_upload_e2e.py
	python scripts/verify_ingest_e2e.py --batch 50

down:
	$(COMPOSE) down

reset-db:          ## destroy and recreate all local data
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f api worker

migrate:
	alembic -c packages/stylist_db/alembic.ini upgrade head

taxonomy:          ## validate config/taxonomy.yaml
	python scripts/validate_taxonomy.py

test:
	pytest -v

lint:
	ruff check packages services tests scripts

fmt:
	ruff format packages services tests scripts

typecheck:
	mypy packages services

# What CI runs, in the same order, so a green `make check` means a green build.
check: taxonomy lint typecheck test
