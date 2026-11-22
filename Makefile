# Uses uv when installed; falls back to the active python otherwise.
UV := $(shell command -v uv 2>/dev/null)
ifeq ($(UV),)
  RUN := python
else
  RUN := uv run
endif

.PHONY: setup reset web chat test evals evals-hard evals-extended compare-models

setup:
ifeq ($(UV),)
	python -m pip install -e ".[dev]"
else
	uv sync
endif

reset:
	$(RUN) python -m bookly.data reset

web:
	$(RUN) uvicorn bookly.web.app:app --port 8000

chat:
	$(RUN) python cli.py --trace

test:
	$(RUN) pytest -q

evals:
	$(RUN) python -m evals.run_evals --set core --runs 3

evals-hard:
	$(RUN) python -m evals.run_evals --set hard --runs 3

evals-extended:
	$(RUN) python -m evals.run_evals --set all --runs 3

compare-models:
	$(RUN) python -m evals.compare_models --runs 3
