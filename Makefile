.PHONY: install test lint format reproduce experiments figures bench readme final holdout-final record docker-build docker-up docker-down docker-logs deploy report teardown teardown-check save-home-profile pull-data

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

# Rebuilds every result from scratch: the tests, then every experiment on the recorded days in
# data-server/ (run make pull-data first), into results/results.json. Held-out data is never read.
reproduce: test experiments figures readme

experiments: install
	uv run python -m touchstone.experiments --data-dir data-server

# README figures, drawn from results/results.json into docs/images.
figures: install
	uv run python -m touchstone.experiments.figures

# The measured numbers in README.md, from results/results.json.
readme: install
	uv run python -m touchstone.experiments.readme

# Replay speed and ring latency again, alone, on each product's last two development days: a few
# minutes, for an otherwise idle machine. Counts and audits from make experiments are kept.
bench: install
	uv run python -m touchstone.experiments.engine_bench --data-dir data-server --last-days 2
	$(MAKE) figures readme

# Once, after the 2026-11-15 00:30 UTC sync: pull the held-out days, evaluate, redraw, fill README.
final: install
	scripts/final.sh

# The held-out window, 2026-11-12 to 2026-11-15, is evaluated once, after the 2026-11-15 sync.
holdout-final: install
	uv run python -m touchstone.experiments.holdout --final --data-dir data-server

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
