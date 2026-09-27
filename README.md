# Opportunity Discovery & Evaluation System

An AI-powered **decision system** that systematically **discovers, researches, evaluates, and develops (as concrete plans)** business opportunities a **solo, capital-constrained founder** can realistically build, launch, and operate.

It is **not** an idea generator and **not** a builder. It exists to answer one question honestly:

> **"Is this opportunity worth spending my time investigating or building?"**

It optimizes for the chain **Evidence → Opportunity → Validation → Economics → Execution → Critical Evaluation**, and explicitly rejects the failure mode **Prompt → AI-generated startup idea**.

---

## Status

Design complete; implementation in progress (Phases 0–3 done, Phase 4 under way; the checklist in [`plan.md`](./plan.md) §35 is the live status). The design is captured across three documents (read in order):

1. **[`intent.md`](./intent.md)** — product & engineering *intent* (the WHAT and WHY): vision, principles, agent architecture, evaluation framework, constraints. The source of truth.
2. **[`specs.md`](./specs.md)** — the implementation-ready *product specification* (the detailed HOW-it-behaves): UX, inputs, data models, agent contracts, run lifecycle, failure handling, MVP scope, and testable acceptance criteria.
3. **[`plan.md`](./plan.md)** — the *engineering plan* (the concrete HOW-to-build): technical architecture, technology choices, repository/DB design, build phases, vertical slices, risk registers, and the first end-to-end milestone.

Choices made where the docs were silent are logged in [`docs/decisions.md`](./docs/decisions.md).

## Running it

```bash
uv sync
uv run alembic upgrade head          # create/upgrade the SQLite database (./data/opp.db)
cp .env.example .env                 # add OPP_TAVILY_API_KEY and an Anthropic key

# Start a run: discovery -> triage -> shallow validation, stopping at the checkpoint.
# Prints every lead (with its triage outcome and sources) and the ranked shortlist.
uv run opp discover --industry HVAC --leads 20
uv run opp leads <run-id>            # show a run's leads and shortlist again later
uv run opp resume <run-id>           # continue a killed/crashed or budget-halted run
uv run opp resume <run-id> --budget 25   # ...raising its ceiling (top-up)
uv run opp recover                   # startup scan: continue runs a dead process left behind

# Offline, from a recorded cassette (no keys needed); --cassette-mode record saves one.
uv run opp discover --industry HVAC --cassette path/to/cassette.json

uv run pytest                        # the whole suite runs offline
```

## How it works (high level)

A **funnel**, not a linear pipeline — buy breadth cheaply, spend depth selectively, attack before scoring:

1. **Discovery** — parallel agents mine real-world *signal sources* (complaints, reviews, job posts, regulatory/tech changes, market inefficiencies) for candidate leads. Every lead must cite a real, retrievable source. LLM brainstorming is banned as a source.
2. **Triage gate** — cheap filtering: dedup, generic-idea hard-kills, and a specificity test (named customer / cited pain / why-now).
3. **Shallow validation gate** — roughly scores the top leads to pick **the one** worth deep research.
4. **Checkpoint** — you approve/veto which opportunity gets the expensive deep dive.
5. **Deep research** — four parallel specialists (customer/demand, competition, market/economics, feasibility & "why now").
6. **Strategy** — turns research into a concrete business concept, MVP, GTM, and small-team operating model.
7. **Red-team → Judge** — an adversarial critic attacks the plan; the judge scores it *after* seeing the attack and issues a verdict: **Pursue / Promising-but-unproven / Park / Kill**.

**Output:** a **1-page decision brief** backed by a full evidence-linked dossier, plus a persisted, promotable shortlist of runners-up.

## Core principles

- **Evidence over intuition** — no opportunity exists until something real was *found*.
- **Never fabricate** — "could not verify" is a valid, first-class answer; invented numbers are forbidden.
- **Facts vs. assumptions are structural** — material claims carry type, source, and confidence.
- **Model, don't assert** — economics are scenario-based with labeled assumptions, never asserted point figures.
- **Cost is first-class** — a hard **$20/run** ceiling; breadth is cheap, depth is selective.
- **Durable state, disposable context** — running out of an agent's context window never ends a run; it resumes from where it left off.
- **Decisions, not numbers** — output maps to an action, not a score to interpret.

## Key constraints (default "Bootstrap / Fast-Cashflow" profile)

| Constraint | Value |
|---|---|
| Team size | 1 person now; up to ~3 acceptable (heavy soft factor) |
| Upfront capital to first revenue | ≤ $2,000 (hard kill) |
| Time to first paying customer | 1–2 months target; hard-kill backstop ~4 months |
| Cost per run | $20 hard ceiling (target $15–18) |

These are **profile parameters**, not global constants — the system is designed to later hunt for "bigger ideas" by swapping the profile, with no rewrite. A leaner **"Micro / Same-Month"** profile (≤ $1,000, revenue within ~1 month) also ships selectable.

## Repository layout

| Path | Purpose |
|---|---|
| [`intent.md`](./intent.md) | Product & engineering **intent** — the WHAT/WHY, source of truth (23 sections) |
| [`specs.md`](./specs.md) | Implementation-ready **product specification** — behavior, data models, acceptance criteria (26 sections) |
| [`plan.md`](./plan.md) | **Engineering plan** — architecture, tech choices, build phases, risks (35 sections) |
| `README.md` | This overview |

## Next steps

- Continue the build from the first unchecked item in [`plan.md`](./plan.md) §35.
- Calibrate real per-run cost against the $20 ceiling on the first ~10 runs (top technical risk).
- Resolve the remaining open questions in [`specs.md`](./specs.md) §26 (e.g., signal-source breadth at MVP, skills-affect-score).
