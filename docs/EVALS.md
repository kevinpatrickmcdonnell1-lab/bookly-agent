# Bookly agent: evals and measured results

How the agent is tested, what the live evals measured, what they found, and the latency work. The [README](../README.md) has the short version.

## How it is tested

| Command | Key? | Covers |
| --- | --- | --- |
| `make test` | No | 199 tests in under 10 s: policy rules and boundary days (30/31, 60/61), every gate precondition (pass and fail), tools, database constraints, router rules and decisions, input and output guards, loop mechanics (rule escalation, gate-denial and round limits, regenerate-once, append-only history), eval harness, eval dashboard loader |
| `make evals` | Yes | 8 core scripted scenarios × 3 runs, asserting on database rows and trace events, never wording. Reports pass rate, refund leakage, escalations by trigger, p50/p95 turn latency and cost per conversation. Add `--judge` for an Opus tone rubric. |
| `make evals-hard` | Yes | The hard scenarios, including finding the item without guessing, pushback after a refusal, a fake approval code, a chargeback threat, a customer naming their own refund amount, someone asking about another customer's order, and two long conversations (6 and 7 turns) with several requests |
| `make evals-extended` | Yes | Every scenario (core, extended and hard) |
| `make compare-models` | Yes | The core suite with each executor setup; add `--set all` for every scenario |

The loop tests use a scripted client to exercise the code around the model. There is deliberately no mock LLM for the demo: without a key, the deterministic layers are what runs.

## Measured results

Measured with `make compare-models --set all` on 2026-11-22: all 29 scenarios × 3 runs per setup (87 conversations each). Router, input guard and supervisor are Haiku 5.5 in every setup. Latency is wall-clock per customer turn, including every model call in that turn.

| Executor setup | Passed | Core | Extended | Hard | p50 / p95 turn latency | Cost per conversation | Refund leakage |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Haiku for all routes | 87/87 | 24/24 | 30/30 | 33/33 | 4.7 s / 8.1 s | $0.0019 | $0.00 |
| Sonnet for all routes | 86/87 | 24/24 | 30/30 | 32/33 | 5.3 s / 10.4 s | $0.0167 | $0.00 |
| Hybrid: Haiku except returns on Sonnet | 87/87 | 24/24 | 30/30 | 33/33 | 5.1 s / 10.3 s | $0.0126 | $0.00 |

Sonnet's one failure is a retrieval gap, not a model error. `search_policy` for "how long do refunds take to reach my card" returns the approval, shipping and store-credit sections, never the return-window section that says "5 to 7 business days". In two runs the agent stated the right timing anyway. In the third, the supervisor rejected the claim because search hadn't returned it, and the customer got the safe fallback reply. See Limitations in the README.

The hard set was added to separate the models. On its first run (Sonnet, before fixes) it scored 26/33 and found two agent problems and one bug in my own scenario:

- **Customer-named amounts broke a valid refund (0/3).** Asked for "$35 back" on a $20 damaged book, the agent proposed $20 and replied "I can't do $35, but…". The output guard rejected $35 as not from a tool, the rewrite repeated it, and the customer got the fallback reply and an escalation instead of their refund. Fixed: a customer-typed amount may now be quoted back only next to a tool-backed amount, and the Haiku supervisor then checks that it isn't promised. Amounts are never whitelisted.
- **Asking when the title already settles it (1/3).** "Order 4843 … when will The Remains of the Day arrive?" The agent checked 4843 (the order used to verify), then asked whether to check 4842. This is the watch item below. It passed in every later run but has no code fix.
- **My assertion was wrong.** I expected the ebooks citation for an undownloaded ebook, but the engine cites the return window. The refund itself was right.

Earlier, on the 8 core scenarios only (24 conversations per setup, same day), the three setups also all passed, at $0.0014, $0.0108 and $0.0098 per conversation.

### Latency work: before and after

The first measurements put the median turn at about 5 s. A breakdown of 96 turns showed the time went on sequential round trips, not slow models. Caching hit on every executor call, and output averaged 86 tokens per round. Three changes targeted that:

1. `verify_customer` now returns a compact summary of recent orders, so the model no longer needs a separate `lookup_orders` round.
2. The Haiku output supervisor is skipped when the policy engine backed the reply that turn. Answers from `search_policy` are still supervised, because those are the model's paraphrase of policy text.
3. The web UI streams trace events and shows progress ("Checking your details…") while a turn runs, and the server warms its API connection at startup. The first request on a cold server had taken about 11 s.

| Core suite, Sonnet executor | Before | After |
| --- | --- | --- |
| p50 / p95 turn latency | 5.1 s / 9.0 s | 4.4 s / 8.5 s (4.1 s / 8.2 s on a repeat run) |
| Executor rounds per turn (mean) | 2.33 | 2.02 |
| `lookup_orders` calls | 15 | 0 |
| Supervisor calls / skipped | 22 / 0 | 5 / 15 |
| Rewrites forced by the output guard | 4 | 1 |
| Cost per conversation | $0.0098 | $0.0108 |

That gain is smaller than I estimated (2.5–3 s). About 1.4 s of every turn is the guard and router, and a typical turn still needs two executor rounds of about 1.5 s each. The next levers are a rule-based fast path that skips the router for bare "yes"/"no" and credential-only messages, and in production a fine-tuned router. Cost rose about 10% because the verify result is larger. One run right after the prompts changed showed $0.0135. That was a one-off rewrite of the prompt cache, and it went away on the next run.

### What this shows, and what it doesn't

- **Haiku matched Sonnet even on the hard set, at about a ninth of the cost and with a lower p95.** The hard set was built to separate them and didn't, once the two agent bugs it found were fixed. My reading: in this design the deterministic layers (router rules, gate, policy engine, output guard) carry the safety, so the executor mainly needs to follow the AOP and call the right tool, and Haiku 5.5 does that. That is evidence for a Haiku default on these flows, not proof. The scenarios are still scripted, and real customers are messier, which is why Sonnet is still the default (README, Key decisions).
- **The hybrid is cheaper than Sonnet but not better than Haiku.** It cost 25% less than Sonnet across all 29 scenarios ($0.0126 vs $0.0167), but it adds model switching mid-conversation, which gives up the prompt cache because caches are per model. With Haiku passing everything, the hybrid's remaining case is risk appetite, not measured quality.
- **Live runs found real bugs, not just model noise.** The first live run scored 71%. The output supervisor rejected true facts from tool results, and the returns AOP asked questions that couldn't change the outcome ("have you read it?" for a damaged book). A later run caught the same kind of AOP problem ("why are you returning it?" for an unread book that isn't a staff pick). Two failures were in my scenarios rather than the agent. Each run writes its full transcripts and traces to `evals/results/` (gitignored). Refund leakage was $0.00 in every run.
- Three runs per scenario is a small sample. 87/87 means "no failures seen in 87 conversations": the true failure rate could still be up to about 4% (95% upper bound), and up to about 14% for a single 24-conversation suite.

