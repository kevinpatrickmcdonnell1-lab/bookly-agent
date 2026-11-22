# Bookly support agent: notes for Claude Code

Thesis: the LLM decides what the customer means; code decides what the customer gets.

## Commands
- `make test` runs the no-key suite (policy, gate, tools, router rules, guards, loop mechanics, eval harness). Keep it under 10 s and green.
- `make web` / `make chat` / `make evals` / `make compare-models` need `ANTHROPIC_API_KEY`.
- `make reset` reseeds `bookly.db` (dates are relative to today).

## Layers (and who can override them)
- `bookly/aops/*.md`, `bookly/prompts/brand_voice.md`: tone and flow. The model follows these.
- `bookly/agent/router.py`: rules first, then a Haiku classifier, then the pure `decide()`.
- `bookly/agent/gate.py`: preconditions for every tool call. The model can't override it.
- `bookly/policy/engine.py`: eligibility, resolution and amount. Pure functions, no I/O.
- `bookly/data/schema.sql`: UNIQUE/CHECK/trigger backstops.

## Rules when changing code
- The executor's `system` prompt and `tools` list must stay byte-identical for a whole session, and history is append-only. Per-turn context goes in a `role: "system"` message (`Agent._append_system`). This keeps the prompt cache warm and keeps thinking blocks valid on the 5.5 models. `tests/test_loop.py::test_full_return_flow_append_only_and_engine_amount` enforces it.
- Never let the model pass a money amount. Amounts come from the policy engine.
- New write tools must be added to `gate.ROUTE_WRITE_TOOLS` and get a passing and a failing gate test.
- On the 5.5 models: no `temperature`, no forced `tool_choice`, and `thinking: disabled` only on Haiku. Model ids live in `bookly/config.py`.
- Every `policy_ref` the engine returns must exist as a `## Heading {#anchor}` section in `bookly/data/kb/`.
