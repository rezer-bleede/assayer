.PHONY: help up down build logs ps shell test migrate clean fmt lint openui-prompt

UNAME := $(shell uname -s)

# macOS only: Docker cannot pass the Apple GPU to containers, so the bundled
# in-Docker model runner would be CPU-only there (~8 tok/s). `make up` skips
# it by default and expects a native Ollama on the host instead
# (README -> "Faster on macOS"). Force the bundled runner with:
#   make up BUNDLED=1
ifeq ($(UNAME),Darwin)
  SKIP_BUNDLED := $(if $(BUNDLED),,--scale ollama=0 --scale ollama-model=0)
endif

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-10s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from example if missing
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

build: env ## Build all images
	docker compose build

up: env ## Start the whole stack (on macOS the bundled model runner is skipped unless BUNDLED=1)
	docker compose up -d --build $(SKIP_BUNDLED)
ifeq ($(UNAME),Darwin)
	@sh -c 'echo > /dev/tcp/127.0.0.1/11434' 2>/dev/null || \
	  echo "note: no native Ollama on 127.0.0.1:11434 — the agent falls back to its deterministic planner. See README -> 'Faster on macOS' (brew install --cask ollama; ollama pull qwen2.5-coder:3b)."
endif

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
