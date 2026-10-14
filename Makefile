# BudgetSwarm dev orchestration (BS-104). See README.md for the full guide.
# On Windows without `make`, use ./scripts/dev-up.sh or the raw docker commands.
.DEFAULT_GOAL := help
COMPOSE := docker compose

.PHONY: help up seed migrate down logs ps

help:
	@echo "make up      - build + start the stack and seed the database (one command)"
	@echo "make seed    - apply migrations + run the offline seed in the backend container"
	@echo "make migrate - apply database migrations only"
	@echo "make down    - stop the stack (data volumes are kept)"
	@echo "make logs    - tail logs for all services"
	@echo "make ps      - show container status"

up:
	$(COMPOSE) up -d --build --wait
	$(MAKE) seed
	@echo "BudgetSwarm is up at https://localhost"

seed:
	$(COMPOSE) exec -T backend python -m scripts.seed_all

migrate:
	$(COMPOSE) exec -T backend python -m alembic upgrade head

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps
