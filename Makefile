.PHONY: setup run test lint format scan demo-seed docker-build

PY := .venv/bin/python

setup:
	uv venv .venv
	uv pip install --python .venv/bin/python -r requirements.txt

run:
	$(PY) -m uvicorn app.main:app --reload --port 8000

test:
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check .

format:
	.venv/bin/ruff format .
	.venv/bin/ruff check --fix .

scan:
	$(PY) -m app.cli scan

demo-seed:
	$(PY) -m app.cli demo-seed

docker-build:
	docker build -t polymarket-edge-finder:local .
