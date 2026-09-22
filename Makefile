.PHONY: setup lock run test lint format scan demo-seed demo-clear docker-build

PY := .venv/bin/python

setup:
	uv venv .venv
	uv pip install --python .venv/bin/python --require-hashes -r requirements.lock

# Re-resolve requirements.txt into the hashed lock that Docker, CI and setup install from.
lock:
	uv pip compile requirements.txt -o requirements.lock --generate-hashes --python-version 3.11 --universal

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

# Synthetic fixture data only; never touches the network. DEMO_MODE=true keeps the seed
# honest even when .env points at a live configuration.
demo-seed:
	DEMO_MODE=true $(PY) -m app.cli demo-seed

demo-clear:
	$(PY) -m app.cli demo-clear

docker-build:
	docker build -t polymarket-edge-finder:local .
