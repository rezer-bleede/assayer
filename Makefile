.PHONY: help up down build logs ps shell test migrate clean fmt lint

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-10s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from example if missing
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

build: env ## Build all images
	docker compose build

up: env ## Start the whole stack
	docker compose up -d --build

down: ## Stop the stack
	docker compose down

restart: ## Restart the stack
	docker compose restart

ps: ## Show service status
	docker compose ps

logs: ## Tail logs
	docker compose logs -f --tail=100

shell: ## Shell into api
	docker compose exec api bash

migrate: ## Run DB migrations
	docker compose exec api python -m app.store.migrate

test: ## Run backend tests
	docker compose exec api pytest -q

fmt: ## Format + autofix
	docker compose exec api ruff format . && docker compose exec api ruff check --fix .

lint: ## Lint only
	docker compose exec api ruff check . && docker compose exec api mypy app

clean: ## Stop + remove volumes
	docker compose down -v