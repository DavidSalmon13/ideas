# specs.md — Product Specification

> **Companion to [`intent.md`](./intent.md).** `intent.md` is the source of truth for *what* and *why*. This document translates that intent into an implementation-ready specification: concrete workflows, data models, state machines, budgets, and acceptance criteria. Where this document refines or improves on `intent.md`, it is flagged inline with **[REFINES INTENT]**.
>
> **Vendor-neutral.** No programming language, framework, database, LLM provider, or search provider is fixed here (see `intent.md` §22). Model *tiers* (cheap / medium / premium) and capability requirements are specified; vendors are not.
>
> **Scope of this deliverable:** specification only. No application code.

---

## Table of Contents

1. Product Overview
2. User Experience
3. User Inputs
4. Opportunity Discovery
5. Multi-Agent System
6. Agent Orchestration
7. Research System
8. Evidence Model
9. Opportunity Data Model
10. Business Model Analysis
11. Small-Team Feasibility
12. Competition Analysis
13. Customer Analysis
14. Financial / Economic Model
15. Validation Plan
16. Red-Team / Criticism
17. Evaluation Framework
18. Final Opportunity Report
19. Research Run Lifecycle
20. Failure Handling
21. Cost and Resource Management
22. Transparency
23. MVP Scope
24. Explicit Non-Goals
25. Acceptance Criteria
26. Open Questions

---

## 1. Product Overview

### 1.1 What it does
A single-operator **decision system** that discovers, researches, and critically evaluates business opportunities, then hands the operator a **1-page decision brief + evidence-linked dossier** for the single best opportunity found in a run, plus a promotable shortlist of runners-up.

### 1.2 Who uses it
One **technical, capital-constrained founder-operator** (see `intent.md` §3). No multi-tenancy, accounts, or sharing in scope. Operator-specific preferences are configuration, not hardcode.

### 1.3 Problem it solves
A solo founder has limited time and money and cannot manually research dozens of opportunities to the depth required to decide which one to pursue. Generic LLM brainstorming produces plausible-but-unsourced ideas that waste that scarce time. This system does *actual grounded research* and returns a **decision**, not a brainstorm.

### 1.4 What the user ultimately gets
For one run (~$15–18, hard-capped at $20): one **decision brief** answering "is this worth my time?", a **dossier** answering "how would I do it?", and a **shortlist** answering "what's next?". Every material claim traces to a real source or is explicitly labeled an assumption.

### 1.5 What makes this different from asking an LLM for startup ideas
| Asking an LLM | This system |
|---|---|
| Invents ideas from priors | Mines real signals; no lead without a real source |
| Confident, unsourced | Structured claims with type + source + confidence |
| One-shot text | Funnel with cheap filtering → selective deep research |
| Rates its own homework | Adversarial red-team **before** the judge |
| Fabricates clean numbers | "Could not verify" is a valid answer; scenario models with labeled assumptions |
| Generic ("AI tool for X") | Mechanical anti-generic hard-gates |
| No decision | Categorical verdict + next experiments |

### 1.6 Core product loop
```
DISCOVER   (mine real signal sources → many leads, each sourced)
  → SCREEN    (dedup + specificity/generic hard-gates + cheap score → top few)
  → SHALLOW-VALIDATE (rough full-framework score → pick THE ONE)
  → [CHECKPOINT: operator approves the expensive dive]
  → RESEARCH  (4 parallel specialists go deep on the one)
  → SYNTHESIZE (merge, reconcile contradictions)
  → DEVELOP   (concrete business + MVP + GTM + small-team ops + economics)
  → ATTACK    (red-team hunts the fatal flaw)
  → EVALUATE  (judge scores after the attack → categorical verdict)
  → PRESENT   (1-page brief + dossier + updated shortlist)
```
This preserves `intent.md` §8. The loop is a **funnel**: breadth is bought cheaply, depth is spent on one survivor.

---

## 2. User Experience

Focus is behavior, not visual styling. The product is usable as a **CLI/TUI or a thin local web UI**; the spec is UI-agnostic. Terms below describe *behaviors* the interface must expose.

### 2.1 Home / landing
On open, the operator sees:
- **Opportunity Library** — all past opportunities across runs, filterable/sortable by verdict, composite score, profile, date, business shape, and status (new / promoted / pursued / discarded).
- **Runs** — past and in-progress runs with status (§19) and cost-to-date.
- **Primary action:** "Start new run."
- **Shortlist queue** — runners-up eligible for promotion to deep research.

### 2.2 Starting a new run
Operator chooses a **discovery mode** (§3):
- **Open-ended** — "find me anything promising for a solo founder" (uses the active Profile as-is).
- **Constrained** — supplies constraints (industry, geography, budget, target revenue, business type, etc.) that form a **RunConfig** layered over the Profile.

### 2.3 Configuring a run
The operator can set/override (all optional; defaults from the active Profile):
- Discovery mode + constraints (§3).
- Profile selection (default: Bootstrap / Fast-Cashflow).
- Run-scoped overrides of gate values (capital ceiling, time-to-revenue) — **[REFINES INTENT]** a run may loosen or tighten gates without altering the standing Profile.
- Budget ceiling for this run (default $20; not raisable above a configured hard maximum without explicit confirmation).
- Checkpoint mode: **mandatory** (default) or **fully autonomous**.
- Number of leads to target at discovery, and shortlist size.

### 2.4 Starting / stopping a run
- **Start** transitions the run to `DISCOVERING` (§19).
- **Stop / Cancel** at any time → run enters `CANCELLED`, emits best partial deliverable (§20), preserves all persisted work.
- **Pause** is implicit: the run halts at `AWAITING_CHECKPOINT` and waits indefinitely.

### 2.5 Viewing progress
Live view shows: current state, active agent(s), sub-task ledger progress, leads discovered so far, running cost vs. budget, and any surfaced contradictions. Progress must be legible without reading raw model output.

### 2.6 Viewing intermediate research
The operator can open any completed stage's structured output mid-run (leads, claims, kill list with reasons, shallow scores). Nothing is hidden until "the end."

### 2.7 The checkpoint (default mandatory)
Before deep research, the run presents the **ranked shortlist** with rough scores and one-line rationales, and asks the operator to **approve / veto / re-pick** which opportunity gets the expensive dive. Each veto/override prompts a **one-line reason** (feeds preference learning, §19 of intent). In autonomous mode, the top-ranked is auto-selected and the reason field is skipped.

### 2.8 Viewing the final result
On `COMPLETED`, the operator sees the **1-page brief** first, with expandable drill-down into the full dossier (§18). Every brief element links to its supporting claims/sources.

### 2.9 Saving opportunities
All opportunities and runners-up persist automatically to the Library (no explicit "save"). The operator can **tag/annotate** and set status (pursue / park / discard).

### 2.10 Comparing opportunities
The operator can select 2+ opportunities and view a side-by-side of per-dimension scores, verdicts, economics snapshots, and small-team feasibility. Comparison uses persisted structured data (no re-computation).

### 2.11 Re-running / deepening
- **Promote** a shortlist runner-up → starts a new deep-research run scoped to that one opportunity (skips discovery/triage), spending a new budget.
- **Refresh** an existing opportunity → re-runs research on stale claims (by timestamp) to update evidence.
- **Deepen** → raises per-agent research budgets for one opportunity and re-runs specialists.

### 2.12 Rejecting / discarding
Operator sets status `discarded` with an optional reason. Discarded opportunities remain in the dedup index (so they are not rediscovered) but are hidden from default Library views.

### 2.13 Potential future functionality (not MVP)
Scheduled autonomous batch runs; cross-run trend dashboards; outcome tracking of pursued ideas; multi-user; export to external tools; the promotable-shortlist auto-promotion policy.

---

## 3. User Inputs

### 3.1 Two modes
**Open-ended discovery** — minimal input; the system explores broadly under the active Profile.
Example: *"Find promising businesses a 1–2 person team could start."*

**Constrained discovery** — the operator supplies constraints that **narrow discovery and act as gates/weights** for this run.
Example: *"B2B businesses in Europe, startable under $20k, potential $30k/month."*

### 3.2 Supported inputs (all optional)
| Input | Effect |
|---|---|
| Industry / vertical | Narrows signal-source targeting; soft filter |
| Geography | Narrows customer/market scope; soft filter |
| Business type / shape | Narrows discovery; soft filter |
| Target customer | Seeds discovery toward a segment |
| Budget / startup capital | Sets the capital **hard gate** for the run |
| Desired revenue / MRR target | Weights revenue-potential dimension; informs economics target |
| Desired profitability / margin | Weights margin dimension |
| Risk tolerance | Adjusts how heavily assumption-risk and defensibility are weighted |
| Time to revenue | Sets/loosens the time-to-revenue gate for the run |
| Available skills / technical expertise | Informs small-team feasibility (what the operator can do vs. must outsource) |
| Time commitment | Informs founder-workload weighting |
| Team size | Sets the min-viable-team target |
| Personal preferences | Free-text steering (soft) |
| Specific constraints | Free-text hard/soft constraints (operator marks which) |
| *(none)* | Pure open-ended discovery |

### 3.3 RunConfig **[REFINES INTENT]**
Inputs compile into a **RunConfig** object that layers over the active **Profile**:
```
RunConfig {
  profile_ref                # base profile (default: Bootstrap/Fast-Cashflow)
  mode                       # open_ended | constrained
  hard_gates_override {      # optional per-run gate values
    max_capital, max_time_to_revenue, ...
  }
  weight_overrides {}        # optional per-dimension weight nudges
  discovery_scope {          # industry, geography, business_shape, target_customer
    ...
  }
  soft_preferences[]         # free-text steering
  budget_ceiling             # default $20
  checkpoint_mode            # mandatory | autonomous
  lead_targets { discovery_n, shortlist_n }
}
```
**Rule:** a RunConfig may relax the default profile's gates for a single run but must state so explicitly (surfaced in the run header and the final report, so a "$20k-capital" run is never confused with a default "$2k" run).

### 3.4 Input validation
- Conflicting constraints (e.g., "$500 capital" + "physical retail store") are allowed but flagged; discovery will likely return few or zero survivors and the run reports that honestly rather than fabricating a fit.
- Empty input is valid (open-ended).

---

## 4. Opportunity Discovery

### 4.1 What constitutes an opportunity
Per `intent.md` §5: a candidate must express a **specific named customer**, a **cited specific pain**, a **specific why-now**, a **plausible small-team wedge**, and a **plausible path to money** — each grounded in real evidence.

### 4.2 Where opportunities come from — signal sources
Discovery runs **parallel signal-source strategies** (extensible set; `intent.md` §9.1):
- Unmet-need / complaint mining
- Incumbent-weakness mining (reviews, complaints about existing tools)
- Regulatory / technology-change mining
- Expensive-manual-process mining (job posts, workflows, services)
- Market-inefficiency / arbitrage mining

Each strategy is a recipe: *where to look, what evidence pattern constitutes a lead, how to extract a structured Lead.* LLM parametric brainstorming is **banned** as a source.

### 4.3 Breadth and counts **[REFINES INTENT — concrete numbers]**
| Stage | Target count | Notes |
|---|---|---|
| Discovery output (leads) | **20–40** across all signal sources | ~4–8 per strategy; snippet-first |
| Survive triage (§4.6) | **5–8** | after hard-gates + dedup + cheap score |
| Enter shallow validation | top **5** | rough full-framework score |
| Selected for deep research | **1** (occasionally 2 if near-tied and budget allows) | the expensive stage |
| Persisted shortlist (runners-up) | remaining shallow-scored leads | promotable later |

These are RunConfig-tunable. **More ideas is not the goal** — the goal is finding opportunities worth investigating; discovery stops when it has enough qualified leads or hits its budget.

### 4.4 Snippet-first retrieval
Discovery reasons over **search-result snippets**, not full page fetches. A full fetch is spent only when a lead has passed cheap screening and needs corroboration. This is the primary cost control at the top of the funnel (`intent.md` §8.4).

### 4.5 Duplicate / similar handling
Every lead is checked against the **dedup index** (past leads, opportunities, and *discarded/killed* items). Near-duplicates are merged (strongest evidence wins) or dropped with a `duplicate` kill reason. Discarded opportunities are never resurfaced.

### 4.6 Filtering (triage) — how generic ideas are rejected
At triage, three **hard-kill** specificity tests (`intent.md` §16) run mechanically:
1. Named-customer test (specific segment).
2. Specific-pain test (cited, documented).
3. Why-now test (specific recent change).
Failing any → auto-kill, logged. A **generic-pattern blocklist** ("AI chatbot for X," "Uber for X," etc.) flags matches for a higher evidence bar rather than auto-killing.

### 4.7 How evidence affects selection
Leads are cheap-scored on source strength, specificity, and obvious small-team fit. Shallow validation then scores the top ~5 on the full framework (§17) using existing snippets plus a tight fetch budget. The **single best** proceeds to deep research.

### 4.8 Progression to deep research
Gated by the **checkpoint** (§2.7). Only after operator approval (or autonomous auto-select) does expensive research begin.

---

## 5. Multi-Agent System

Agents are roles executed by an LLM at a specified tier, orchestrated by a non-LLM **Orchestrator** (§6). Each agent below lists: purpose, responsibilities, inputs, outputs, allowed/forbidden, depth, tools, communication, evaluation, stop condition, and no-evidence behavior.

> **[REFINES INTENT]** The architecture matches `intent.md` §9. The operator's alternate list (Discovery, Coordinator, Market/Customer/Competition Researchers, Business Model Analyst, Financial Analyst, Execution Analyst, Red-Team, Final Evaluator) is *mostly* covered — but several of those (Market/Customer/Competition/Business-Model/Financial) are unified as the **four Deep-Research Specialists + Strategy agent** to avoid agent sprawl and stay within the $20 budget. A separate "Research Coordinator" is replaced by the deterministic Orchestrator (cheaper, more reliable than an LLM coordinator).

### 5.1 Discovery Agent (cheap tier; parallel instances, one per signal source)
- **Purpose:** mine one signal source for leads.
- **Responsibilities:** run the source recipe; extract structured Leads; attach ≥1 real source each.
- **Inputs:** RunConfig (scope, soft prefs), learned preferences, dedup index.
- **Outputs:** Leads (§9).
- **Allowed:** snippet search, limited fetch (within budget).
- **Forbidden:** inventing leads; unsourced leads; deep analysis.
- **Depth:** shallow; breadth-oriented.
- **Tools:** web search (snippet), limited fetch.
- **Communication:** writes Leads to DB; no direct agent-to-agent messaging.
- **Evaluated by:** triage (survival rate, specificity, source quality).
- **Stops when:** target lead count reached or source budget exhausted.
- **No evidence:** emits fewer/zero leads; never fabricates to hit a quota.

### 5.2 Triage Agent (cheap tier)
- **Purpose:** filter leads before expensive work.
- **Responsibilities:** apply specificity hard-gates; apply generic blocklist; dedup; cheap-score; select survivors; log kills with reasons.
- **Inputs:** all Leads, dedup index, RunConfig.
- **Outputs:** ranked survivors; Kill records (§9).
- **Forbidden:** deep research; promoting a lead that fails a hard-gate.
- **Depth:** minimal.
- **Stops when:** all leads classified.
- **No evidence:** a lead lacking required sourced claims fails the gate (that *is* the mechanism).

### 5.3 Shallow Validation Agent (medium tier; tight budget)
- **Purpose:** rough full-framework scoring to pick the one.
- **Responsibilities:** for each of the top ~5, produce provisional per-dimension scores + rationale using existing snippets plus ≤ a small fetch budget per lead; rank; select the survivor(s); persist runners-up as shortlist.
- **Inputs:** ranked survivors, RunConfig, framework.
- **Outputs:** shortlist with provisional scores; selected opportunity.
- **Forbidden:** full deep research; fabricating economics.
- **Stops when:** all candidates scored and ranked.
- **No evidence:** scores the dimension low with `evidence: insufficient`; does not guess.

### 5.4 Deep-Research Specialists ×4 (medium tier; parallel; bounded)
One agent per axis; each produces structured claims (§8):
- **(a) Customer & Demand** — see §13.
- **(b) Competition & Existing Solutions** — see §12.
- **(c) Market & Economics/Spend** — market structure/size, existing spend, unit-economic anchors (real prices/costs).
- **(d) Feasibility, Timing & Why-Now** — technical feasibility, barriers, the specific recent change, realistic small-team scope.
- **Inputs:** the selected Opportunity (lead + all prior claims), axis assignment, per-agent research budget.
- **Outputs:** structured Claims on the axis.
- **Allowed:** search + fetch within budget; cross-referencing.
- **Forbidden:** straying outside the axis; fabricating figures; exceeding the fetch budget.
- **Depth:** deep on one axis only.
- **Communication:** write claims to DB; may raise a **blocking-gap** kickback (§6).
- **Evaluated by:** validation step (§7.5) and Synthesis.
- **Stops when:** axis questions answered to sufficiency (§7.6) or budget exhausted.
- **No evidence:** records `unknown` claims with reasons; flags load-bearing unknowns.

### 5.5 Synthesis Agent (medium tier)
- **Purpose:** merge the four specialist reports into one coherent picture.
- **Responsibilities:** consolidate claims; **surface contradictions** between specialists (do not smooth them); reconcile confidence.
- **Inputs:** all specialist claims.
- **Outputs:** unified research section; contradictions list.
- **Forbidden:** resolving a contradiction by inventing a tiebreak fact.
- **Stops when:** all claims consolidated.

### 5.6 Strategy / Execution Agent (premium tier)
- **Purpose:** turn research into a concrete business.
- **Responsibilities:** what to build; MVP; business model; pricing; distribution + acquisition; GTM; **small-team operating model** (§11); automation/outsourcing map; **scenario economics** (§14); scaling; moats; **what must be validated** (§15).
- **Inputs:** synthesized research, RunConfig, small-team constraints.
- **Outputs:** execution plan, unit-economics model, load-bearing assumptions, validation backlog.
- **Forbidden:** asserted point financials; ignoring hard gates.
- **Stops when:** plan complete.
- **No evidence:** builds explicit assumptions, labeled and fed to the validation backlog.

### 5.7 Red-Team / Critic Agent (premium tier)
- **Purpose:** attack the opportunity; find the reason it fails.
- **Responsibilities:** find the fatal flaw; seek disconfirming evidence; challenge load-bearing assumptions; argue why incumbents may already cover it; expose hidden team/capital needs; §16.
- **Inputs:** full plan + all claims + economics.
- **Outputs:** ranked attacks; `fatal_flaw` flag if warranted.
- **Allowed:** additional targeted verification fetches (small budget).
- **Forbidden:** rubber-stamping; attacking with fabricated counter-facts (attacks are themselves evidence-graded).
- **Stops when:** the top plausible failure modes are covered.

### 5.8 Judge Agent (premium tier)
- **Purpose:** score the framework **after** reading plan + attack; issue verdict.
- **Responsibilities:** per-dimension 1–5 scores with justification; composite (ranking only); apply non-compensatory hard-gate/fatal-flaw overrides; produce categorical verdict + 1-page brief inputs.
- **Inputs:** plan, claims, red-team attack, framework, profile weights.
- **Outputs:** Evaluation record (§9); brief.
- **Forbidden:** compensating a fatal flaw with strengths; inventing confidence.
- **Stops when:** verdict issued.
- **Cannot confidently evaluate:** returns verdict `Promising-but-unproven` (or `Park`) with an explicit statement of what evidence is missing — never a false confident verdict (§20).

---

## 6. Agent Orchestration

### 6.1 Deterministic orchestrator
A non-LLM **Orchestrator** owns the state machine (§19), budgets (§21), checkpoints, kickbacks, persistence, and recovery. This is cheaper and more reliable than an LLM coordinator.

### 6.2 Order and parallelism
- **First:** Discovery agents (parallel across signal sources).
- **Sequential gates:** Triage → Shallow Validation → Checkpoint.
- **Parallel:** the 4 Deep-Research Specialists (for the one survivor).
- **Sequential:** Synthesis → Strategy → Red-Team → Judge.

### 6.3 Requesting additional research (bounded kickback)
Any downstream agent may raise a **blocking-gap** ("customer segment undefined — can't assess willingness to pay"), returning the opportunity **one stage** for a targeted pass. **Cap: 1 kickback per stage per opportunity**, logged, with a hard budget stop (`intent.md` §19.1). No unbounded loops.

### 6.4 Agents challenging each other
The Red-Team explicitly challenges the Strategy/Judge chain. Specialists do not debate each other directly; contradictions are surfaced by Synthesis and, if load-bearing, can trigger one kickback.

### 6.5 Branching
Discovery branches across many leads (breadth). After the checkpoint, the pipeline commits to **one** opportunity (occasionally two). It does **not** branch into parallel deep dives — that would break the $20 budget.

### 6.6 When an opportunity is abandoned
- At triage: fails a hard-gate or is a duplicate.
- At shallow validation: ranks below the survivor(s) → parked as shortlist.
- At red-team/judge: fatal flaw or hard-gate breach → `Kill`.
Each is logged with a reason.

### 6.7 When expensive research happens
Only after the checkpoint, on the single selected opportunity, with per-agent budgets.

### 6.8 Disagreement resolution
- Specialist vs. specialist: Synthesis surfaces the contradiction; if load-bearing, one kickback for a tiebreaking source; if still unresolved, the claim is carried as **low-confidence with the conflict noted** and the Judge penalizes accordingly.
- Red-Team vs. Strategy: the Judge adjudicates, having seen both.

### 6.9 Sufficiency
Research is "sufficient" when the opportunity's **decision-critical claims** (those feeding hard gates and the top-weighted dimensions) are answered to at least `medium` confidence, or explicitly marked `unknown` with the reason and a validation experiment queued. See §7.6.

### 6.10 Cost/time control
Per-stage and per-agent budgets (§21); per-stage pre-checks; snippet-first; model tiering; dedup reuse.

### 6.11 State-machine view
See §19. The orchestration is that state machine plus the kickback edges.

---

## 7. Research System

### 7.1 Search behavior
Snippet-first (§4.4). Full fetch only when a claim needs corroboration or a lead has earned depth. Every agent has a fetch budget.

### 7.2 Source gathering & evaluation
Each retrieved source is recorded as a **Source** (§8) with type, publisher, dates, and a **credibility tier**:
| Tier | Examples |
|---|---|
| High | Government/regulatory filings, primary data, reputable industry reports, official pricing pages |
| Medium | Established trade press, well-known review platforms, vendor docs |
| Low | Forums, social posts, anonymous reviews, blogs |
Low-tier sources are valid **evidence of sentiment/pain** but cannot alone support a `fact`-typed quantitative claim.

### 7.3 Cross-checking
A quantitative `fact` should be **corroborated by ≥2 independent sources** where feasible; single-source quantitative claims are capped at `medium` confidence. `corroboration_count` is tracked.

### 7.4 Evidence extraction: fact vs. assumption
Every material statement becomes a **Claim** typed `fact | estimate | assumption` (§8). A `fact` without a retrievable source is **auto-demoted to `assumption`**.

### 7.5 Validation step (anti-hallucination)
Before a stage's output is accepted, an automated check verifies: every `fact` has a source; confidence has a stated reason; and computes the section's **assumption load** (share of decision-critical claims that are `assumption`/low-confidence). High assumption-load sections are flagged and down-weighted by the Judge.

### 7.6 Research completeness / sufficiency
An axis is complete when its decision-critical questions are each either answered at ≥ `medium` confidence or marked `unknown` with a reason and a queued validation experiment. Non-critical gaps do not block completion.

### 7.7 Agent behavior by evidence state
| Situation | Required behavior |
|---|---|
| Evidence strongly supports | `fact`, `high` confidence, ≥2 sources, timestamped |
| Evidence weakly supports | `estimate` or `fact`+`low/medium`, note the weakness |
| Evidence conflicts | Surface both sources, mark `conflicted`, lower confidence, note the disagreement; do not pick silently |
| No evidence | `unknown` (or labeled `assumption` if an inference is genuinely needed), never a fabricated figure; queue a validation experiment |
| Making an inference | `assumption` with stated reasoning and confidence; explicitly not a fact |

### 7.8 Freshness
Every source carries `published_date` (when known) and `accessed_date`. Claims older than a configurable staleness threshold are flagged; **Refresh** (§2.11) re-verifies them. "Why-now" claims must be recent by construction.

---

## 8. Evidence Model

### 8.1 Claim
```
Claim {
  id
  statement                  # human-readable
  type                       # fact | estimate | assumption
  value?, unit?              # for quantitative claims (e.g., 30000, USD/month)
  sources[]                  # Source refs; REQUIRED for type=fact
  confidence                 # high | medium | low
  confidence_reason          # REQUIRED
  evidence_strength          # strong | moderate | weak
  stance                     # supporting | opposing | neutral   (relative to the opportunity)
  corroboration_count        # # independent sources
  conflicts[]                # refs to contradicting claims
  timestamp_observed         # when the agent recorded it
  agent, stage               # provenance
  notes?
}
```

### 8.2 Source
```
Source {
  id
  url
  title
  source_type                # regulatory | industry_report | trade_press | review | forum | vendor | pricing_page | job_post | other
  publisher?
  published_date?
  accessed_date
  credibility_tier           # high | medium | low
  excerpt                    # the snippet/quote the claim rests on
}
```

### 8.3 Traceability rule
Every element of the final report links to the Claim(s) behind it, and every `fact` links to its Source(s). The report must be fully traceable **evidence → claim → conclusion**.

---

## 9. Opportunity Data Model

Domain model (not a DB schema). Fields marked *(claim-typed)* are Claims (§8), carrying their own evidence and confidence.

```
Opportunity {
  id, run_id, lead_ref
  title
  status                     # new | promoted | pursued | parked | discarded
  business_shape             # saas | service | marketplace | data | licensing | infoproduct | ecommerce | physical | rollup | arbitrage | infra | other
  profile_ref, run_config_ref
  stage_state                # nested run→stage→agent→subtask position (§19)

  problem {                  # the pain
    description (claim)
    frequency (claim), severity (claim)
  }
  customer {                 # §13
    buyer (claim), user (claim), segment (claim)
    current_behavior (claim), current_spend (claim)
    discovery_channels (claim), decision_maker (claim)
    acquisition_difficulty (claim), switch_motivation (claim), objections[]
  }
  market {                   # §10, §14
    structure (claim), size (claim), growth (claim), existing_spend (claim)
  }
  existing_solutions[]       # what customers do today
  competition[]              # §12: Competitor records
  why_now (claim)            # the specific recent change
  proposed_business {
    what_to_build, value_prop, wedge
  }
  product_mvp {
    scope, must_haves[], explicitly_excluded[]
  }
  business_model {
    revenue_model, pricing (claim-anchored)
  }
  economics {                # §14
    unit_model
    scenarios { conservative, base, upside }
    load_bearing_assumptions[]     # drive validation backlog
    metrics { cac?, ltv?, gross_margin?, payback?, break_even?, capital_required?, time_to_revenue?, time_to_profitability? }  # each claim-typed w/ verified|estimated|assumed|unknown
  }
  distribution { channels[], acquisition_strategy }
  small_team_feasibility {   # §11
    min_viable_team, required_skills[], founder_responsibilities[],
    automation_map[], ai_automation_map[], outsourcing_map[],
    blockers_to_solo[], workload_assessment,
    can_one_person_start (claim + rationale),
    can_1_3_operate (claim + rationale)
  }
  risks[]                    # from red-team
  assumptions[]              # all labeled assumptions, ranked by leverage
  red_team { attacks[], fatal_flaw? }
  validation_backlog[]       # §15: Experiment records, ranked
  evaluation {               # §17
    dimension_scores[], weights_ref, composite, verdict, justification
  }
  evidence { claims[], sources[] }   # full provenance
  created_at, updated_at
}
```

Supporting records:
```
Competitor { name, type(direct|indirect|substitute), pricing(claim), positioning, strengths[], weaknesses[], complaints[](claim), gap(claim) }
Experiment { hypothesis, assumption_ref, method(interview|landing_page|presale|outreach|mvp|desk_research), cost_estimate, time_estimate, success_criteria, priority }
Kill { lead_or_opp_ref, stage, reason, failed_gate?, timestamp }
Lead { see intent §11.1 }
```

---

## 10. Business Model Analysis

For each opportunity the Strategy agent (with specialist inputs) investigates, each value **explicitly tagged** `verified | estimated | assumed | unknown`:
- Customer, pain, current solution, existing spending, willingness to pay
- Pricing, revenue model
- Gross margin, operating costs
- Customer acquisition (channel + difficulty), sales cycle
- Retention, churn, expansion potential
- Market size, market growth
- Distribution, scalability
- Capital requirements

**No fake precision:** every number is a Claim with type + confidence; unknowns stay unknown and become validation items. A value tagged `assumed` must state the assumption's basis.

---

## 11. Small-Team Feasibility

A dedicated, first-class analysis (`intent.md` §18). For each opportunity:
- **Minimum viable team** (1 / 2 / 3) with rationale.
- **Required skills** and whether the operator has them (from RunConfig) or must outsource.
- **Founder responsibilities** and estimated **operational workload**.
- **Sales / support / technical / physical / capital** requirements.
- **Automation map** (software), **AI-automation map**, **outsourcing map**.
- **Blockers to solo operation** — explicit list.

The system must explicitly answer, each as a Claim with rationale:
> **"Can 1 person realistically start this?"**
> **"Can 1–3 people realistically operate this at the initial stage?"**

If **no**, the analysis states exactly which requirement breaks it (e.g., "requires 24/7 support," "needs field sales," "regulated activity requiring licensed staff"). Small-team feasibility is a **heavily weighted soft** dimension — a `min_team = 3` opportunity is penalized but not auto-killed unless it also breaches a hard gate.

---

## 12. Competition Analysis

The Competition specialist identifies and analyzes:
- **Direct** competitors, **indirect** competitors, **substitutes** (including "the manual status quo").
- Pricing, positioning, distribution, strengths, weaknesses.
- **Customer complaints / reviews** (evidence of pain and of solution limitations).
- **Market gaps** and **potential differentiation**.

**Interpretation rule:** competition is **not automatically negative**. Existing competitors that customers pay are *positive evidence of willingness to pay and an existing budget*. The analysis distinguishes "crowded with weak incumbents + specific unmet need" (attractive) from "saturated with strong, cheap incumbents" (unattractive). This feeds both the competitive-intensity dimension and the "why customers would switch" claim.

---

## 13. Customer Analysis

The Customer specialist attempts to determine, each claim-typed:
- Who **buys** vs. who **uses**.
- The problem, its **frequency**, and its **pain severity**.
- What they **currently do** and **currently spend**.
- How they **discover** solutions.
- Who makes the **buying decision**.
- **Acquisition difficulty** and channels.
- **Why they would switch** and likely **objections**.

Evidence sources: real complaints, reviews, forum threads, job posts, community discussions, pricing pages. Sentiment from low-tier sources is valid for pain existence but not for quantitative spend claims (§7.2).

---

## 14. Financial / Economic Model

**Model, don't assert** (`intent.md` §17). For each serious opportunity:
- Build a **minimal unit-economics model** (e.g., price × conversion × volume − costs), **anchored** to real data found (competitor pricing, tool/ad costs).
- Reason about: revenue, pricing, gross profit/margin, opex, CAC, LTV, payback period, break-even, capital requirements, time to revenue, time to profitability, scaling economics.
- Run **three scenarios** — `conservative | base | upside` — each stating the assumptions that drive it.
- Identify **load-bearing assumptions** (1–3 inputs the outcome is most sensitive to) → these become the **top validation experiments** (§15).

**Rules:** every input is a Claim (`verified | estimated | assumed | unknown`); no single asserted profit number; no multi-year fake-precision projections; ranges over points when uncertain. The Judge scores whether even the **conservative** scenario clears the profile's bar and whether load-bearing assumptions are cheaply testable.

---

## 15. Validation Plan

Every promising opportunity ends with a **ranked validation backlog** answering: *"What should I do next to find out whether this is real?"*

For each item:
- The **assumption/unknown** it tests (linked to a load-bearing assumption where possible).
- **Risk/leverage** (how much the decision hinges on it).
- **Cheapest validation method**: customer interviews, landing-page test, pre-sale, outreach experiment, MVP experiment, or desk research.
- **Cost + time estimate** and **success criteria**.

Ordering: highest-leverage × cheapest-to-test first. The system never concludes "this is a good idea" without stating what remains unproven and how to prove it.

---

## 16. Red-Team / Criticism

A dedicated adversarial pass (§5.7) that actively hunts for failure, investigating: why customers might not buy; why the market may be smaller than it looks; copyability; acquisition cost; margin erosion; operational difficulty at scale; hidden regulatory issues; technical problems; timing risk; dependency/platform risk; saturation; and false assumptions.

- Output: a **ranked attack list**, each attack itself evidence-graded (an attack backed by a real source outweighs speculation).
- A sufficiently severe, well-supported attack sets `fatal_flaw`, which forces a `Kill` at judging (non-compensatory).
- **Goal:** reduce **false positives**, not kill good ideas. The Judge sees the attacks and weighs them against the plan's evidence.

---

## 17. Evaluation Framework

### 17.1 Multi-dimensional, not a single arbitrary score
Per-dimension scores (1–5) with written justification; a composite used **only for ranking**; and a **categorical verdict** as the decision payload. Non-compensatory.

### 17.2 Dimensions
Revenue potential · Gross margin · Time-to-first-revenue · Upfront capital · Automation/solo-operability · Small-team feasibility · Customer-acquisition difficulty · Competitive intensity · Defensibility/moat · Why-now/timing · Market attractiveness · Customer pain · Willingness to pay · Scalability · **Evidence quality** · **Assumption risk**.

### 17.3 Weights
Weights come from the active **Profile** (`intent.md` §13.2), adjustable per RunConfig. Profiles form a selectable tier ladder:

| Profile | Capital hard gate | Time-to-revenue hard gate | Emphasis |
|---|---|---|---|
| **Micro / Same-Month** (leanest) | > **$1,000** → kill | > **~1 month** → kill | Fastest, cheapest cashflow; market/moat de-prioritized |
| **Bootstrap / Fast-Cashflow** (default) | > **$2,000** → kill | > **~4 months** → kill | Fast profit; light moat tolerated |
| Balanced / Ambitious (future) | relaxed | relaxed | Larger ceiling, defensibility |

The default Bootstrap profile up-weights time-to-revenue, margin, automation, and capital and down-weights TAM and long-term defensibility; **Micro / Same-Month** pushes those weights further and de-prioritizes market size, growth, and scalability almost entirely. Adding a profile is **configuration only** — no code/architecture change (a single new profile record + gate/weight values).

### 17.4 Scoring rules
- Each dimension score must cite the claims supporting it; a dimension resting on `unknown`/low-confidence claims is capped (cannot score high on absent evidence).
- **Evidence quality** and **assumption risk** are themselves dimensions — a well-argued but thinly-sourced opportunity scores lower on these and thus lower overall.
- **Penalties / overrides (non-compensatory):** any hard-gate breach (capital > ceiling, time-to-revenue > backstop) or a red-team `fatal_flaw` forces `Kill` regardless of composite.

### 17.5 Composite
A profile-weighted aggregate of dimension scores, **for ranking the shortlist only**. It is displayed but is never the decision.

### 17.6 Verdict
| Verdict | Meaning / operator action |
|---|---|
| **Pursue** | Evidence + economics strong enough to start now |
| **Promising-but-unproven** | Could work; hinges on load-bearing assumptions X/Y → run the named experiments first (expected to be the most common, correct answer) |
| **Park** | Real but weak/blocked; kept for later |
| **Kill** | Disqualified by hard-gate breach or fatal flaw; logged with reason |

### 17.7 Interpretation
The report explains what each dimension score means and *why*, so the operator is never handed an uninterpretable number.

---

## 18. Final Opportunity Report

Layered (`intent.md` §20). Layer 1 is read to **decide**; Layer 2 is used to **act**.

### 18.1 Layer 1 — 1-page Decision Brief
1. **Verdict + one-line thesis** — *"[Specific customer] pays for [X]; a solo founder can capture it because [why-now + why small-team-able]."*
2. **Opportunity** — 2–3 sentences: what it is, who pays, for what.
3. **Why now** — the specific recent change + confidence.
4. **Economics snapshot** — base-case one-liner + conservative scenario; 1–3 load-bearing assumptions flagged.
5. **Small-team feasibility** — min viable team + biggest threat to solo operation.
6. **Competition + why customers switch** — one line each.
7. **Kill risks** — red-team's top 1–3, honestly stated.
8. **Cheapest next experiment** — the single highest-leverage, cheapest test + rough cost/time.
9. **Confidence + evidence quality** — overall + "% resting on unverified assumptions."

### 18.2 Layer 2 — Full Dossier
Sections (each evidence-linked): **Executive Summary · Opportunity · Problem · Customer · Why Now · Market · Existing Solutions · Competition · Proposed Business · Product/MVP · Business Model · Pricing · Distribution · Customer Acquisition · Economics (all scenarios + assumptions) · Small-Team Feasibility · Risks · Assumptions · Evidence (claims + sources) · Validation Experiments · Execution Roadmap · Critical Analysis (red-team) · Evaluation (per-dimension scores + justification) · Recommended Next Steps.**

### 18.3 Layer 3 — Promotable Shortlist
Runners-up from shallow validation, ranked by provisional score, each promotable to a full deep run later.

### 18.4 Incomplete runs
If a run halts early (§20), the report is emitted marked **"INCOMPLETE — halted at stage X,"** verdict capped at `Promising-but-unproven`, listing which axes were never examined. Honesty extends to the run's own completeness.

---

## 19. Research Run Lifecycle

### 19.1 States
```
CREATED
  → DISCOVERING
    → SCREENING              (triage)
      → SHALLOW_VALIDATION
        → AWAITING_CHECKPOINT   (skipped in autonomous mode)
          → DEEP_RESEARCH       (4 specialists, parallel)
            → SYNTHESIZING
              → BUILDING_PLAN    (strategy)
                → RED_TEAM
                  → EVALUATING   (judge)
                    → COMPLETED
```
**Terminal/side states:** `CANCELLED`, `HALTED_BUDGET`, `HALTED_ERROR`, `COMPLETED_PARTIAL`.

### 19.2 Nested checkpointing
State is nested: **run → stage → agent → sub-task**, each independently persisted (`intent.md` §21). A partial run is a first-class, resumable object.

### 19.3 Transitions & special edges
- **Kickback edge:** any post-research stage → previous stage, once per stage (§6.3).
- **Budget pre-check:** before any expensive stage; insufficient budget → `HALTED_BUDGET`.
- **Checkpoint:** `AWAITING_CHECKPOINT` waits for operator approve/veto/re-pick (or auto-select).

### 19.4 Failure states
- **`HALTED_BUDGET`** — budget exhausted; stop-and-wait; emit best partial; operator may top-up-and-resume or accept partial.
- **`HALTED_ERROR`** — unrecoverable external failure after retries; emit best partial; resumable from last committed sub-task.
- **Context-window overflow is NOT a failure state** — handled by automatic compaction + continuation within the same stage (§20, `intent.md` §21.2).

### 19.5 Cancellation, retry, partial, resume
- **Cancel** → `CANCELLED`, best partial emitted, work preserved.
- **Retry** → transient external failures retried with backoff (§20); sub-task-level.
- **Partial completion** → `COMPLETED_PARTIAL` with the incomplete-run report (§18.4).
- **Resume** → always from the last committed sub-task, rehydrating from compact persisted state (not raw history).

---

## 20. Failure Handling

Principle: **degrade gracefully; never pretend research is complete.**

| Failure | Handling |
|---|---|
| Search fails | Retry w/ backoff; try alternate query/source; if persistent, record `unknown` for affected claims and continue |
| API fails | Retry w/ backoff; fail over to alternate provider if configured; else `HALTED_ERROR` w/ partial output |
| Agent times out | Abort agent; keep its committed sub-tasks; retry once; else mark axis incomplete and continue with gap noted |
| Poor agent output | Validation step (§7.5) rejects unsourced/low-quality claims; one kickback for a redo; persistently poor → carry gaps as `unknown` |
| Sources not found | Claim → `unknown`; queue a validation experiment; never fabricate |
| Sources conflict | Surface both, mark `conflicted`, lower confidence, Judge penalizes (§6.8) |
| Agent hallucinates | Caught by validation (unsourced `fact` auto-demoted; §7.5); flagged; excluded from scoring support |
| Research incomplete | Report marked incomplete (§18.4); verdict capped |
| Two agents disagree | §6.8 resolution; unresolved → low-confidence + noted conflict |
| Evaluator not confident | Verdict `Promising-but-unproven`/`Park` + explicit missing-evidence statement; never a false confident verdict |
| **Context-window overflow** | **Automatic compaction + fresh-context continuation from the work ledger; resume at next sub-task; never ends the run** (`intent.md` §21.2) |
| Budget exhausted | `HALTED_BUDGET`; stop-and-wait; best partial (§19.4) |

---

## 21. Cost and Resource Management

### 21.1 Hard ceiling
**$20/run** hard cap; target **$15–18**. Raising above a configured maximum requires explicit operator confirmation.

### 21.2 Target budget allocation **[REFINES INTENT — concrete]**
| Stage | Model tier | Target |
|---|---|---|
| Discovery | cheap | $3–4 |
| Triage | cheap | <$1 |
| Shallow validation | medium | ~$3 |
| Deep research (4 specialists, 1 opp) | medium | $5–7 |
| Synthesis | medium | ~$1 |
| Strategy | premium | ~$1 |
| Red-team + Judge | premium | ~$2–3 |
| Reserve | — | ~$2 |

### 21.3 Controls
- **Per-stage budget pre-checks** — never start a stage the remaining budget can't afford.
- **Per-agent research budgets** — hard caps on fetches/tokens; no single agent can silently overspend.
- **Snippet-first retrieval** — full fetch only when earned.
- **Model tiering** — cheap for volume, premium for the one survivor's high-value reasoning.
- **Parallel execution bounded** by remaining budget.
- **Retry caps** — bounded retries with backoff; retries counted against budget.
- **Prompt caching** — static assets (signal recipes, framework, profile) cached.
- **DB dedup** — never re-research known/rejected items; reuse prior claims within staleness window.
- **Staged funnel** — cheap research filters before expensive deep research (the core cost strategy).

### 21.4 Cost accounting
Every agent call, search, and fetch is metered and attributed to a stage; running cost is visible live (§2.5) and recorded on the run.

---

## 22. Transparency

The operator must be able to see how a conclusion was reached. The UI/report exposes:
- **Sources** (with tier, dates, excerpt) behind every material claim.
- **Research findings** as structured claims, filterable by type/confidence.
- **Key assumptions**, ranked by leverage.
- **Confidence** and **evidence quality** per claim and overall.
- **Unknowns** — explicitly listed, not hidden.
- **Contradictions** — surfaced, with the conflicting sources.
- **Agent conclusions** per stage.
- **Kill reasons** — why any lead/opportunity was rejected.
- **Progression reasons** — why the selected opportunity advanced.

**Rule:** AI conclusions are never presented as unquestionable fact. Every conclusion is qualified by its evidence and confidence, and is traceable to sources.

---

## 23. MVP Scope

**MVP goal:** prove the system can **discover and deeply investigate genuinely interesting, specific, evidence-backed opportunities** and return a trustworthy decision. Be aggressive about cutting everything else.

### 23.1 MVP (must-have)
- End-to-end funnel: Discovery → Triage → Shallow Validation → (checkpoint) → Deep Research (4 specialists) → Synthesis → Strategy → Red-Team → Judge.
- **Two selectable profiles** — Micro / Same-Month (leanest: ≤$1k, ~1mo) and Bootstrap / Fast-Cashflow (default) — plus open-ended and basic constrained modes (RunConfig with capital/time gates + discovery scope). Additional profiles are config-only.
- Structured **Claim/Source** evidence model with the no-fabrication + auto-demote rules and the validation step.
- Anti-generic hard-gates + generic blocklist.
- Scenario economics with load-bearing assumptions → validation backlog.
- Non-compensatory evaluation → categorical verdict.
- Layered output: 1-page brief + full dossier; kill log.
- Persistent Library + dedup index.
- Cost metering + $20 ceiling + per-stage/per-agent budgets + snippet-first + model tiering.
- Run state machine with **context-overflow auto-recovery** and budget stop-and-wait; resumable runs.
- Mandatory checkpoint (a simple approve/veto/pick interaction is sufficient; CLI prompt acceptable).

### 23.2 V1 (after core works)
- Preference learning (soft steering) + exploration allowance.
- Promotable-shortlist promotion flow; Deepen/Refresh.
- Opportunity comparison view.
- Additional signal-source strategies; richer constrained-mode inputs.
- Richer live-progress UI; full transparency drill-downs.
- Alternate profiles ("Balanced," "Ambitious").

### 23.3 Future
- Scheduled autonomous batch discovery.
- Outcome tracking + closed-loop weight learning.
- Cross-run trend dashboards.
- Knowledge-graph evidence model.
- Multi-user / sharing / export.

---

## 24. Explicit Non-Goals

The first version does **NOT**:
- Build the product, write production code, or deploy anything (planning only).
- Guarantee financial accuracy — it produces **evidence-graded estimates**, not audited figures.
- Support multiple users, accounts, auth, or sharing.
- Provide legal, tax, or regulatory advice (it may *flag* regulatory risk as a research finding).
- Automatically execute validation experiments (it designs them; the operator runs them).
- Auto-promote or auto-pursue opportunities without operator action.
- Optimize for large-TAM/venture-scale ideas by default (default profile is bootstrap-oriented).
- Continuously run on a schedule (MVP is on-demand).
- Guarantee exhaustive discovery — it optimizes for *enough qualified leads*, not coverage.

---

## 25. Acceptance Criteria

Concrete, testable. The implementation is successful only if:

### 25.1 Opportunity discovery
- A run produces **20–40 leads** (or reports honestly why fewer, e.g., over-constrained RunConfig).
- **Every lead cites ≥1 real, retrievable source.** A lead with no source is impossible to produce.
- Leads are deduped against prior runs and discarded items; no discarded opportunity is resurfaced.

### 25.2 Research quality
- Every material claim is typed `fact | estimate | assumption` with a confidence + reason.
- No `fact` exists without a retrievable source (auto-demotion verified by the validation step).
- Quantitative `fact`s are corroborated by ≥2 independent sources **or** capped at `medium` confidence.
- Given a query with no supporting evidence, the system returns `unknown`, not a fabricated figure. **(Testable: seed a run where key data cannot exist; assert no invented numbers appear.)**

### 25.3 Evidence handling
- The final report is fully traceable: every conclusion → claim(s) → source(s).
- Contradictions between sources/specialists are surfaced, not silently resolved.
- Assumption load per section is computed and shown.

### 25.4 Agent orchestration
- Discovery signal sources run in parallel; the 4 specialists run in parallel; gates run in order.
- Deep research occurs only after the checkpoint, on ≤2 opportunities.
- Kickbacks are capped at 1 per stage per opportunity and are logged.
- A run never exceeds the $20 ceiling; per-stage pre-checks prevent starting unaffordable stages.

### 25.5 Business analysis
- Every serious opportunity has each business-model value tagged `verified | estimated | assumed | unknown`.
- No un-tagged bare numbers appear in the dossier.

### 25.6 Financial analysis
- Economics are expressed as **conservative/base/upside** scenarios with stated driving assumptions.
- Load-bearing assumptions are identified and each maps to a validation experiment.
- No single asserted profit figure and no multi-year fake-precision projection appears.

### 25.7 Small-team analysis
- Every opportunity explicitly answers "Can 1 person start this?" and "Can 1–3 operate it initially?" with rationale.
- A "no" states the specific breaking requirement.

### 25.8 Red-team analysis
- Every opportunity that reaches evaluation has a ranked red-team attack list.
- A well-supported fatal flaw forces a `Kill` regardless of composite score.

### 25.9 Final evaluation
- Output is per-dimension scores + composite (ranking only) + one categorical verdict.
- Hard-gate breach (capital > ceiling; time-to-revenue > backstop) forces `Kill`.
- No dimension scores high on `unknown`/low-confidence evidence.

### 25.10 Final report
- Every completed run emits a 1-page brief (9 elements) + full dossier, evidence-linked.
- Every brief claim links to supporting evidence in the dossier.

### 25.11 Failure handling
- Budget exhaustion → `HALTED_BUDGET`, best partial emitted, resumable.
- Context-window overflow → automatic recovery; the run continues and completes. **(Testable: force an agent to exceed context; assert the run resumes at the next sub-task and completes without loss beyond one sub-task.)**
- Unrecoverable external failure → `HALTED_ERROR`/`COMPLETED_PARTIAL` with an incomplete-marked report; never a falsely "complete" result.

---

## 26. Open Questions

### 26.1 How broad should the default signal-source set be at MVP?
- **Why it matters:** more sources = more breadth but more discovery cost and complexity.
- **Options:** (a) all 5 archetypes; (b) start with 2–3 strongest (unmet-need + incumbent-weakness + manual-process).
- **Recommendation:** **(b)** for MVP — the three highest-signal, lowest-noise sources — then expand in V1.
- **Would help decide:** early runs measuring lead yield/quality per source.

### 26.2 Should shallow validation ever forward 2 opportunities instead of 1?
- **Why it matters:** two deep dives roughly double the most expensive stage and may breach $20.
- **Options:** (a) strictly 1; (b) allow 2 only when top-2 are near-tied *and* projected budget permits.
- **Recommendation:** **(a) for MVP** (simplicity + budget safety); revisit in V1.
- **Would help decide:** observed frequency of near-ties; actual deep-research cost variance.

### 26.3 CLI/TUI vs. thin web UI for MVP?
- **Why it matters:** affects build effort; the checkpoint + live progress need *some* interface.
- **Options:** (a) CLI/TUI; (b) minimal local web UI.
- **Recommendation:** **(a) CLI/TUI** for MVP (fastest to prove the engine); (b) in V1 for Library/comparison ergonomics.
- **Would help decide:** how often the operator wants to browse/compare vs. just run.

### 26.4 How is source credibility tier assigned?
- **Why it matters:** it gates whether a source can support a `fact`.
- **Options:** (a) static domain/type heuristics; (b) an LLM classification per source; (c) hybrid.
- **Recommendation:** **(c) hybrid** — cheap static rules by `source_type`/domain, LLM only for ambiguous cases.
- **Would help decide:** observed misclassification rate and its cost impact.

### 26.5 What exactly triggers the generic-pattern blocklist "higher bar," and how high?
- **Why it matters:** too strict kills real opportunities that superficially resemble generic ones; too loose lets generics through.
- **Options:** (a) require ≥2 high-tier corroborated pain claims; (b) require a quantified existing-spend claim; (c) both.
- **Recommendation:** **(a)** at MVP; add (b) if generics still leak.
- **Would help decide:** false-positive/false-negative rates on a labeled test set of leads.

### 26.6 Staleness threshold for claim freshness / Refresh?
- **Why it matters:** balances evidence currency against re-research cost.
- **Options:** fixed (e.g., 90 days); per-claim-type (why-now shorter, market-size longer).
- **Recommendation:** **per-claim-type**, with conservative defaults, tunable.
- **Would help decide:** how fast the targeted domains actually change.

### 26.7 Should the operator's skills materially change scoring, or only the feasibility narrative?
- **Why it matters:** an opportunity "hard for most" may be easy for this operator (or vice versa).
- **Options:** (a) skills only annotate feasibility text; (b) skills adjust the small-team-feasibility dimension score.
- **Recommendation:** **(b)** — the product is a *personal* decision tool; the operator's actual skills should move the score, transparently.
- **Would help decide:** whether the operator wants portable/objective scores or personalized ones (leans personalized per intent §3).

### 26.8 Deferred to intent: knowledge-graph evidence model and outcome-based learning
- Both are explicitly future (`intent.md` §12, §19). Open until there is (a) a need for cross-run entity reasoning and (b) enough real pursued-idea outcomes to learn from.
```
