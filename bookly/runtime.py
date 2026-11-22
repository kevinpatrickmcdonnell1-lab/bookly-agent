"""Wiring shared by the CLI, the web app and the evals: one run_turn() for all three."""

from __future__ import annotations

import os
import sys

from bookly.agent.loop import Agent
from bookly.config import Settings, get_settings
from bookly.data.repo import Repo


def require_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        sys.stderr.write("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key.\n"
                         "(`make test` needs no key.)\n")
        raise SystemExit(1)


def build_agent(settings: Settings | None = None, repo: Repo | None = None, llm=None) -> Agent:
    settings = settings or get_settings()
    repo = repo or Repo(settings.db_path)
    repo.ensure()
    if llm is None:
        from bookly.llm import AnthropicLLM
        llm = AnthropicLLM(fallbacks=settings.executor_fallbacks)
    return Agent(settings, repo, llm)
