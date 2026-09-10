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

# The test DSNs are set HERE, not left to the shell. conftest defaults to
# localhost:5432/stylist_test, but the pgvector container publishes 55432 — so
# a bare `pytest` silently targets whatever Postgres happens to be on 5432 and
# fails with `extension "vector" is not available`, which reads like a broken
# migration rather than a misdirected connection. Setting them makes the
# target correct on its own.
TEST_OWNER_DSN := postgresql://stylist_owner:stylist_owner_local_only@localhost:55432/stylist_test
TEST_APP_DSN   := postgresql+asyncpg://stylist_app:stylist_app_local_only@localhost:55432/stylist_test

# Redis/S3 too: the api and idempotency fixtures SKIP when they cannot reach
# redis, and skipped tests exit 0 — so a bare `pytest` reports a green
# "17 skipped" that looks exactly like a pass. Ports match the compose file's
# non-standard host mappings.
TEST_ENV := MIGRATION_DATABASE_URL="$(TEST_OWNER_DSN)" \
            DATABASE_URL="$(TEST_APP_DSN)" \
            REDIS_QUEUE_URL="redis://localhost:63790/0" \
            REDIS_CACHE_URL="redis://localhost:63791/0" \
            S3_BUCKET=stylist-local S3_ENDPOINT_URL=http://localhost:9000 \
            S3_REGION=us-east-1 S3_ACCESS_KEY=minioadmin S3_SECRET_KEY=minioadmin \
            MODELS_ROOT="$(PWD)/models" \
            U2NET_HOME="$(PWD)/models/u2net"

test:
	$(TEST_ENV) pytest -v

test-strict:       ## fail if anything SKIPPED — catches a misconfigured env
	$(TEST_ENV) pytest -q -rs --strict-markers
	@echo "note: review the 's' lines above; a skip is not a pass"

lint:
	ruff check packages services tests scripts

fmt:
	ruff format packages services tests scripts

typecheck:
	mypy packages services

# What CI runs, in the same order, so a green `make check` means a green build.
check: taxonomy lint typecheck test
