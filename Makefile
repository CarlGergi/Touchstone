.PHONY: install test lint format reproduce record docker-build docker-up docker-down docker-logs deploy report teardown teardown-check save-home-profile pull-data

install:
	uv sync

test: install
	uv run pytest

lint: install
	uv run ruff check .
	uv run ruff format --check .
	uv run shellcheck -x scripts/*.sh scripts/server/*.sh

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

deploy:
	scripts/deploy.sh

report:
	scripts/report.sh

# Stops recording, checks every file is in S3, then deletes the instance and its disk. Run this
# only after 2026-12-08. It refuses to run before then.
teardown:
	scripts/teardown.sh

# Rehearses the teardown without changing anything.
teardown-check:
	scripts/teardown.sh --check

save-home-profile:
	scripts/save-home-profile.sh

pull-data:
	scripts/pull-data.sh
