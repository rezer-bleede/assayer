.PHONY: help up up-local-model down build logs ps shell test migrate clean fmt lint openui-prompt

UNAME := $(shell uname -s)

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-10s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from example if missing
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

build: env ## Build all images
	docker compose build

up: env ## Start the stack (bundled Ollama is opt-in)
	@docker compose --profile local-model stop ollama ollama-model >/dev/null 2>&1 || true
	docker compose up -d --build
ifeq ($(UNAME),Darwin)
	@sh -c 'echo > /dev/tcp/127.0.0.1/11434' 2>/dev/null || \
	  echo "note: no native Ollama on 127.0.0.1:11434; local inference is unavailable unless another LLM provider is configured. See README -> 'Run Ollama on the host'."
endif

up-local-model: env ## Start the stack with the bundled Ollama model runner
	LLM_BASE_URL=http://ollama:11434/v1 docker compose --profile local-model up -d --build

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

openui-prompt: ## Regenerate the OpenUI system prompt + spec from the web library
	cd web && npx --no-install openui generate lib/openui/library.tsx \
	  --export assayerLibrary --no-interactive \
	  -o ../api/app/agent/prompts/openui.md

clean: ## Stop + remove volumes
	docker compose down -v
