# Convenience wrappers around docker compose. On Windows without `make`, run the
# commands directly — every recipe here is a single line on purpose.
.PHONY: up up-obs down build logs shell test test-int lint typecheck migrate eval fmt

up:          ## build + start api, worker, postgres, redis  (http://localhost:8000/docs)
	docker compose up --build -d

up-obs:      ## same, plus Prometheus (:9090) and Grafana (:3000)
	docker compose --profile observability up --build -d

down:        ## stop containers, keep the postgres volume
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f api worker

shell:       ## a python shell inside the api container
	docker compose exec api python

test:        ## unit tests only — no network, no DB, no model weights
	docker compose run --rm api pytest

test-int:    ## integration tests against the real Postgres + Redis
	docker compose run --rm api pytest -m integration

lint:
	docker compose run --rm api ruff check app tests evals

fmt:
	docker compose run --rm api ruff format app tests evals

typecheck:
	docker compose run --rm api mypy app

migrate:     ## make migrate M="add run_steps table"
	docker compose exec api alembic revision --autogenerate -m "$(M)"
	docker compose exec api alembic upgrade head

eval:        ## run the golden set and write evals/runs/<timestamp>.json
	docker compose run --rm api python -m evals.run_eval
