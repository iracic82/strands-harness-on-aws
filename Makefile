REGION ?= us-west-2

.PHONY: test lint doctor
test:
	uv run pytest
lint:
	uv run ruff check . && uv run ruff format --check .
doctor:
	uv run python doctor.py --region $(REGION)
