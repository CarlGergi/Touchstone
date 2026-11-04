.PHONY: install test lint format reproduce record docker-build docker-up docker-down docker-logs

install:
	uv sync

test: install
	uv run pytest

lint: install
	uv run ruff check .
	uv run ruff format --check .

format: install
	uv run ruff check --fix .
	uv run ruff format .

# Rebuilds every result in the repository from scratch. Later milestones add their experiments here.
reproduce: test

record: install
	uv run python -m touchstone.recorder run --data-dir data

docker-build:
	docker compose build

docker-up:
	UID=$$(id -u) GID=$$(id -g) docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f --tail=100
