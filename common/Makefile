
.PHONY: help install test clean

help:
	@echo "Available targets:"
	@echo "  install - Install package in editable mode"
	@echo "  test    - Run tests with coverage"

install:
	@echo "Installing package in editable mode..."
	UV_PROJECT_ENVIRONMENT=../.venv uv sync --inexact

test:
	@echo "Running tests with coverage..."
	UV_PROJECT_ENVIRONMENT=../.venv uv run pytest --cov=metta.common --cov-report=term-missing

clean:
	@echo "Cleaning extra venv..."
	@rm -rf .venv
	@rm -f uv.lock

