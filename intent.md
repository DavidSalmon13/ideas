# intent.md — Opportunity Discovery & Evaluation System

> **Status:** Source-of-truth product/engineering intent. Describes the WHAT and WHY, and the high-level HOW. Implementation details (languages, frameworks, databases, model vendors, infra) are intentionally left open except where they are necessary to express product intent.
>
> **Document type:** Living design contract. Any implementation should be traceable back to a decision in this document. Where this document says a mechanism "must" exist, that is a requirement; where it says "default" or "recommended," that is a starting point that may be tuned via configuration.

---

## 1. Product Vision

An AI-powered **decision system** that systematically **discovers, researches, evaluates, and develops (as concrete plans)** potentially profitable business opportunities that a **very small team — realistically one person — can build, launch, and operate**.

The system does not merely generate business ideas. It exists to answer a single, sharper question for its operator:

> **"Is this opportunity worth spending my time investigating or building?"**

It optimizes for the chain:

> **Evidence → Opportunity → Validation → Economics → Execution → Critical Evaluation**

and explicitly rejects the failure mode:

> **Prompt → AI-generated startup idea**

The output of the system is a **decision brief plus an evidence-backed dossier** for the single most promising opportunity found in a run, accompanied by a ranked shortlist of runners-up that can be investigated later. It is a research and reasoning instrument, not an idea generator and not a builder.

**Explicitly out of scope:** the system does *not* build the product, write production code, deploy anything, or execute the business. "Develop" means *develop the idea into a concrete, validated plan*, not *develop software*. Building is left to the human operator and separate tooling.

---

## 2. Core Objective

**Primary optimization target:** Given real-world evidence, surface the single opportunity with the best **risk-adjusted expected value for a solo, capital-constrained operator**, and present it honestly enough that the operator can decide whether to spend their time on it.

"Best" is defined by the **active Opportunity Profile** (§13.2), which encodes the operator's current goals. The default profile favors **fast, cheap paths to real profit** over large-but-unreachable markets.

The system is judged by one standard: **would a smart, skeptical solo founder, reading the output, make a better decision about where to spend their time than they would have without it?** Everything below serves that standard.

---

## 3. Target User

A **single technical founder-operator** (initially, the author) who:

- Is capable of building software and reasoning about business/technical tradeoffs (output may assume a sophisticated reader; no hand-holding or onboarding required).
- Is **solo** and **capital-constrained** (see §6).
- Wants **small opportunities that can become profitable quickly and cheaply**, and who values automation, low customer-support burden, and low ongoing workload.

**Design stance (personal tool now, not precluding a product later):**

- Build as a **single-operator personal tool**. Do **not** build multi-tenancy, accounts, auth, or sharing.
- **However**, all operator-specific preferences (what counts as "attractive") must be expressed as **configurable parameters / profiles**, not hardcoded constants, so the system could later serve other users or hunt for different classes of opportunity without a rewrite.

---

## 4. Core Principles

These principles govern every implementation decision. When a tradeoff arises, resolve it in favor of these.

1. **Evidence over intuition.** No opportunity exists until something real was *found*. The system reasons *over* retrieved evidence; it does not invent opportunities from the model's parametric knowledge.
2. **Never fabricate.** The system must never present an invented number or fact as real. Unknown is a valid, first-class answer. It is always better to say "could not verify" than to produce a clean, false figure.
3. **Facts and assumptions are structurally distinct.** Material claims carry their own type, source, and confidence (§12). "Distinguish facts from assumptions" is enforced mechanically, not aspirationally.
4. **Specific over generic.** The system hunts for narrow, named, defensible opportunities and mechanically rejects vague ones (§16).
5. **Model, don't assert.** Financial reasoning is done through transparent, scenario-based models with labeled assumptions — never through asserted point figures (§17).
6. **Cost is a first-class constraint.** A run has a hard budget. Breadth is bought cheaply; depth is spent selectively (§6, §8).
7. **Attack before you score.** No opportunity is evaluated favorably until it has been deliberately attacked by a red-team (§14).
8. **Durable state, disposable context.** An agent's LLM context is disposable working memory. The source of truth is the persisted structured record. Running out of context must never end a run (§21).
9. **Decisions, not numbers.** Output maps to an action the operator will take (pursue / validate / park / kill), not to a score they must interpret.
10. **Honest about itself.** The system reports its own confidence, evidence quality, and completeness — including when a run halted early or a claim is unverified.
11. **Small-team feasibility is first-class.** Whether 1–3 people can realistically run the business is evaluated explicitly, not as an afterthought.
12. **Learn the operator's taste, but don't trap them.** Preferences are learned as soft steering, never hard filters, with a deliberate exploration allowance (§19).

---

## 5. Opportunity Definition

A candidate qualifies as a **potentially attractive opportunity** only if it can express, each backed by real evidence:

- **A specific, named customer segment** — narrow and identifiable (e.g., "independent HVAC contractors in the US doing under $1M revenue"), not "small businesses" or "developers."
- **A specific, documented pain** — a *cited* instance of that segment expressing the pain (a real complaint, review, forum post, job listing, pricing page, etc.), not a hypothesized pain.
- **A specific "why now"** — a concrete recent change (regulation, new API/technology, price shift, behavior change, supply/demand shift) that opened this opportunity, ideally within the last ~1–2 years. "AI is getting better" is not a why-now.
- **A plausible small-team wedge** — a narrow entry point a solo founder could actually start with, not "build a platform."
- **A plausible path to money** — someone who plausibly pays, for something, at a price that could clear the economics under the active profile.

Opportunities may be **any business shape** — niche SaaS, automated/software-enabled service, specialized agency, marketplace, data/licensing business, information product, e-commerce, niche physical product, acquisition/roll-up, arbitrage, B2B workflow automation, infrastructure, or something enabled by an emerging technology. The system is **not** constrained to venture-backed startup concepts. The objective is an *attractive business*, not a fundable startup.

The first three tests (named customer / cited specific pain / specific why-now) are **hard gates** at triage (§16).

---

## 6. Constraints

### 6.1 Operator constraints (default profile)

| Constraint | Value | Enforcement |
|---|---|---|
| **Team size** | 1 person now; up to ~3 acceptable | Heavily weighted, **soft** (a genuinely exceptional 3-person opportunity may survive) |
| **Upfront capital to first revenue** | **≤ $2,000** | **Hard kill** if exceeded |
| **Time to first paying customer** | **1–2 months target** | **Heavy soft penalty** beyond 2 months; **hard-kill backstop at ~4 months** |

These values are **profile parameters** (§13.2), not global constants. The default **Bootstrap / Fast-Cashflow** profile uses the values above. Future profiles (e.g., "Balanced," "Ambitious") may relax them.

### 6.2 System constraints

| Constraint | Value |
|---|---|
| **Cost per run** | **Hard ceiling $20; target $15–18** |
| **Internet / live retrieval** | Assumed always available; no offline fallback required |
| **Fabrication** | Forbidden (Principle 2) |
| **Per-agent research budget** | Every agent has an explicit, configurable ceiling on retrieval (fetches / tokens). No single agent may silently overspend. |
| **Kickback loops** | Max 1 per stage per opportunity (§19) |

### 6.3 The tension these constraints create (and how it is resolved)

Breadth of discovery, research depth, evidence quality, avoiding generic ideas, and small-team profitability all pull against the **$20 ceiling**. This is resolved structurally (§8) by:

- Buying **breadth cheaply** (snippet-first retrieval, cheap models, parallel discovery).
- Spending **depth selectively** — full deep research runs on **essentially one opportunity per run**.
- Reusing prior research via the persistent database (never re-researching known/rejected things).

There is **no pretense** that the ceiling and "3 deep plans per run" coexist. The system produces **one** deep plan per run plus a promotable shortlist (§8, §20).

---

## 7. Research Philosophy

Research quality is the product. The system must **perform actual research and reasoning**, not summarize the model's priors. Every agent that produces material claims must:

1. **Discover** evidence from real, retrievable sources.
2. **Investigate** it (go deeper than the first result).
3. **Cross-reference** it (corroborate across independent sources).
4. **Identify contradictions** between sources and surface them rather than smoothing them over.
5. **Separate facts from assumptions** using the structured-claim model (§12).
6. **Quantify** where possible — but only through honest modeling (§17), never fabricated figures.
7. **Identify uncertainty** explicitly and attach confidence with a stated reason.
8. **Challenge its own conclusions** and actively **search for disconfirming evidence** before concluding.
9. **Build conclusions from evidence**, not from plausibility alone.

**Grounding rule (non-negotiable):** No lead may enter the funnel, and no material claim may be asserted, without at least one **real, retrievable source**. LLM parametric brainstorming is **banned** as a source of opportunities.

**Honesty rule:** When evidence cannot be found, the correct output is an explicit "unverified / could not find data," carried as a low-confidence assumption — never an invented figure.

---

## 8. Agent Architecture

The operator's initial proposal (three sequential researchers + one judge, committing to a single idea up front) is **replaced**. A naive linear pipeline is incompatible with the operator's own stated requirements — branch into multiple opportunities, filter before expensive research, generate and rank many ideas, control cost. Those requirements demand a **funnel**: cheap broad generation → cheap filtering → expensive depth only on survivors → adversarial evaluation.

### 8.1 The pipeline

```
        ┌─────────────────────────── PERSISTENT OPPORTUNITY DATABASE ───────────────────────────┐
        │  (leads, claims+sources, verdicts, kill-reasons, dedup index, operator preferences)     │
        └───────────────────────────────────────────────────────────────────────────────────────┘

STAGE 1  DISCOVERY  (breadth · cheap · parallel across signal sources · snippet-first)
            │  emits many structured LEADS, each with ≥1 real source
            ▼
STAGE 2  TRIAGE GATE  (cheap · scoring + dedup + specificity hard-gates + generic blocklist)
            │  kills weak/generic/duplicate leads (logged with reasons); passes top ~5
            ▼
STAGE 2.5 SHALLOW VALIDATION  (medium · tight fetch budget · rough full-framework scoring)
            │  picks THE ONE opportunity worth deep research; ranks the rest as shortlist
            ▼
  ╔══════════ CHECKPOINT (default: mandatory) ══════════╗
  ║  Operator reviews ranked shortlist BEFORE the        ║
  ║  expensive stage. Approve / veto / re-pick.          ║
  ║  (Optional fully-autonomous mode skips this.)        ║
  ╚══════════════════════════════════════════════════════╝
            ▼
STAGE 3  DEEP RESEARCH  (depth · 1 survivor · 4 parallel specialists · mid-tier model · bounded)
            │  (a) Customer & Problem/Demand
            │  (b) Competition & Existing Solutions
            │  (c) Market Size & Economics/Spend
            │  (d) Feasibility, Timing & "Why Now"
            ▼
STAGE 3.5 SYNTHESIS  (merges specialist findings into one coherent research picture)
            ▼
STAGE 4  STRATEGY / EXECUTION  (premium model · concrete business concept, MVP, GTM, ops model)
            ▼
STAGE 5  ADVERSARIAL EVALUATION  (premium)
            │  5a. RED-TEAM / CRITIC  — attacks the opportunity, hunts the fatal flaw
            │  5b. JUDGE  — scores the framework AFTER seeing the plan AND the attack
            ▼
        FINAL OUTPUT: 1-page decision brief + full dossier + updated shortlist in DB
```

### 8.2 Why this architecture (vs. the original proposal)

- **Funnel, not line.** Generate many, filter cheaply, spend depth on survivors — satisfies breadth + cost + anti-generic simultaneously.
- **Three gates, one deep dive.** Triage (Stage 2) and shallow validation (Stage 2.5) ensure the expensive Stage 3 is spent on **one** opportunity, keeping runs under $20.
- **Specialist decomposition of deep research.** Splitting "deep research" into four parallel specialists is faster *and* higher quality — each goes deep on one axis and cannot paper over a weak axis with a strong one.
- **Red-team before judge.** A judge scoring un-attacked work rates its own team's homework. Separating attack from scoring is the single biggest quality improvement over the original single-judge design.
- **Persistent DB + dedup.** Compounds value across runs and drives cost down over time.
- **Bounded iteration.** Downstream agents can kick an opportunity back one stage for a specific missing thing, capped to prevent cost spirals (§19).

### 8.3 Parallelism

- **Stage 1** discovery strategies run in **parallel** (leads are independent).
- **Stage 3** specialists run in **parallel** for the single survivor (axes are independent).
- Stages are otherwise sequential. Parallelism is bounded by the per-run cost budget.

### 8.4 Cost discipline (how $20 is held)

- **Snippet-first retrieval** — reason over search-result snippets during breadth; only pay to fetch full pages once a lead/claim has earned it. (Largest single saving.)
- **Model tiering** — cheap model for discovery + triage; medium for shallow validation + deep-research specialists; premium reserved for strategy, red-team, judge (one opportunity only).
- **Per-agent research budgets** — hard ceilings on fetches/tokens per agent.
- **Prompt caching** — cache static assets (signal-source recipes, framework definitions, profile).
- **DB dedup** — never re-research what's already known.
- **Per-stage budget pre-checks** — never start a stage the remaining budget can't afford (§21).

---

## 9. Agent Responsibilities

Each agent is defined by purpose, inputs, and outputs. Models named are *tiers*, not vendors.

### 9.1 Discovery Agent(s) — Stage 1 (cheap tier, parallel)
- **Purpose:** Mine a specific **signal source** for candidate opportunities.
- **Signal-source archetypes (initial, extensible set):**
  - **Unmet-need / complaint mining** (forums, reviews, communities expressing pain).
  - **Incumbent-weakness mining** (reviews/complaints about existing solutions' limitations).
  - **Regulatory / technology-change mining** (recent changes creating new needs or enabling new solutions).
  - **Expensive-manual-process mining** (job postings, workflows, services revealing costly manual work).
  - **Market-inefficiency / arbitrage mining** (supply/demand gaps, distribution gaps, price disparities).
- **Inputs:** active Opportunity Profile; learned operator preferences (soft); dedup index.
- **Outputs:** structured **Leads** (§11.1), each with ≥1 real source. No lead without a source.
- **Constraint:** snippet-first; parametric brainstorming forbidden.

### 9.2 Triage Agent — Stage 2 (cheap tier)
- **Purpose:** Cheaply filter leads before any expensive research.
- **Responsibilities:** apply the **specificity hard-gates** (§16); apply the **generic-pattern blocklist**; dedup against the DB; cheap-score survivors (source strength, specificity, obvious small-team fit); pass the top ~5.
- **Outputs:** ranked surviving leads; **kill list with reasons** (persisted).

### 9.3 Shallow Validation Agent — Stage 2.5 (medium tier, tight budget)
- **Purpose:** Roughly score the top ~5 leads on the full evaluation framework (§13) to select **the one** to take deep.
- **Outputs:** rough per-dimension scores + a **ranked shortlist**; the selected opportunity; runners-up persisted as promotable.

### 9.4 Deep Research Specialists — Stage 3 (medium tier, parallel, bounded)
Four specialists, each producing structured claims (§11.2) on one axis:
- **(a) Customer & Problem/Demand** — exact customer, pain intensity, existing workflows, willingness/ability to pay, existing spending.
- **(b) Competition & Existing Solutions** — competitors, pricing, limitations, why customers would switch, why incumbents haven't solved it.
- **(c) Market Size & Economics/Spend** — market structure and size, existing spend, distribution, unit-economic anchors (real prices/costs).
- **(d) Feasibility, Timing & "Why Now"** — technical feasibility, barriers to entry, the specific recent change, what a small team can realistically do.

### 9.5 Synthesis Agent — Stage 3.5 (medium tier)
- **Purpose:** Merge the four specialist reports into one coherent research picture; surface contradictions between specialists; consolidate claims and confidence.
- **Outputs:** unified research dossier section with reconciled claims and flagged conflicts.

### 9.6 Strategy / Execution Agent — Stage 4 (premium tier)
- **Purpose:** Turn synthesized research into a concrete business concept.
- **Responsibilities:** what exactly to build; the MVP; business model; pricing; distribution + customer-acquisition strategy; initial GTM; **small-team operating model** (§18); what's automated / outsourced / requires expertise; **scenario-based economics** (§17); scaling path; moats; and **what must be validated before building**.
- **Outputs:** the execution plan portion of the dossier, including the **unit-economics model** and the **load-bearing assumptions**.

### 9.7 Red-Team / Critic Agent — Stage 5a (premium tier)
- **Purpose:** Attack the opportunity. Its only job is to find the reason this fails.
- **Responsibilities:** find the fatal flaw; search for disconfirming evidence; challenge load-bearing assumptions; explain why incumbents may already have this covered; identify hidden reasons it might require far more than 1–3 people or more than $2k.
- **Outputs:** structured list of attacks, ranked; any **fatal-flaw** finding is flagged.

### 9.8 Judge Agent — Stage 5b (premium tier)
- **Purpose:** Score against the framework **after** reading both the plan and the red-team attack, and issue the categorical verdict (§14).
- **Outputs:** per-dimension scores + justifications; composite (for ranking only); **categorical verdict**; the 1-page brief inputs.

### 9.9 Orchestrator (system, not an LLM role)
- **Purpose:** Manage the pipeline state machine, budgets, checkpoints, kickbacks, persistence, and recovery (§21). Enforces per-stage budget pre-checks and the $20 ceiling.

---

## 10. Agent Handoffs

Each agent receives only what it needs, always as structured records from the DB (not raw prior context):

| To | Receives |
|---|---|
| **Triage** | All Leads from Discovery (structured, with sources) + dedup index + profile |
| **Shallow Validation** | Surviving ranked leads + profile + learned preferences |
| **(Checkpoint)** | Ranked shortlist + rough scores → operator |
| **Deep Research specialists** | The one selected opportunity: its lead, all its claims/sources so far, its axis assignment. Specialists do **not** start from scratch — they extend existing evidence. |
| **Synthesis** | All four specialist reports (structured claims + sources) |
| **Strategy** | Synthesized research picture + profile + small-team constraints |
| **Red-Team** | Full plan + all research claims + economics model |
| **Judge** | Full plan + all claims + **red-team attack** + framework + active profile weights |

**Handoff rule:** handoffs carry **compact structured records** (claims, summaries, work ledgers), never raw page text or raw model transcripts. This is what keeps context bounded and runs resumable (§21).

---

## 11. Research Output Structure

All agent outputs are structured records persisted to the DB.

### 11.1 Lead (Discovery output)
```
Lead {
  id
  title                     # short, specific
  named_customer            # structured claim (must be specific)
  specific_pain             # structured claim, cited
  why_now                   # structured claim, cited
  candidate_wedge           # short description
  business_shape            # SaaS / service / marketplace / ...
  sources[]                 # ≥1 real, retrievable
  signal_source             # which archetype found it
  discovered_at             # timestamp
}
```

### 11.2 Structured Claim (used everywhere material)
See §12. Every claim feeding evaluation is a structured object with type, source, confidence, and reason.

### 11.3 Opportunity Record (accumulates through the pipeline)
```
Opportunity {
  id, lead_ref
  stage_state               # run→stage→agent→subtask position (§21)
  research {
    customer[], competition[], market_economics[], feasibility_timing[]   # structured claims
    contradictions[]        # surfaced conflicts
  }
  economics {
    unit_model, scenarios{conservative, base, optimistic}, load_bearing_assumptions[]
  }
  execution {
    what_to_build, mvp, business_model, pricing, gtm, acquisition,
    operating_model (§18), automation_map, outsourcing_map, scaling, moats
  }
  red_team { attacks[], fatal_flaw? }
  judgment { dimension_scores[], composite, verdict, justification }
  validation_backlog[]      # experiments, derived from load-bearing assumptions
  provenance                # all sources, timestamps
}
```

### 11.4 Kill Record
```
Kill { lead_or_opp_ref, stage, reason, failed_gate, timestamp }
```
Persisted so the system does not rediscover and re-reject the same thing, and so decisions are auditable.

---

## 12. Evidence & Sources

**Model: structured claims with typed provenance**, applied to all **material** claims (anything feeding evaluation: market size, spend, pricing, willingness-to-pay, competitor facts, why-now triggers, capital/time estimates). Narrative/connective text may remain prose.

```
Claim {
  statement
  type            # fact | estimate | assumption
  sources[]       # required for `fact`; a fact without a source is auto-demoted to `assumption`
  confidence      # high | medium | low
  confidence_reason   # why this confidence (required)
  timestamp       # when observed/retrieved
}
```

**Enforceable rules:**
- A `fact` **must** carry a retrievable source, or it is mechanically demoted to `assumption`.
- Confidence must state a reason.
- Every material claim carries a timestamp.
- A validation step (§15) programmatically rejects unsourced facts and flags assumption-heavy sections.
- The Judge down-weights plans that rest heavily on low-confidence assumptions.

A **queryable knowledge graph** across all runs (full entity/relationship linking) is **deferred** as future work; the structured-claim model is the v1 backbone.

---

## 13. Opportunity Evaluation Framework

### 13.1 Dimensions (scored 1–5, each with written justification)
- Revenue potential
- Gross margin
- Time to first revenue
- Upfront capital required
- Automation / solo-operability potential
- Small-team feasibility (§18)
- Customer-acquisition difficulty
- Competitive intensity
- Defensibility / moat
- "Why now" / timing strength
- Evidence quality
- Assumption risk (how much rests on unvalidated load-bearing assumptions)

### 13.2 Opportunity Profiles (first-class, swappable)

A **Profile** is a configuration object that **re-targets the entire funnel** — which signal sources discovery emphasizes, triage thresholds, dimension **weights**, and **hard gates**. Profiles are how the system's definition of "attractive" is expressed and how it scales to bigger ideas later.

**Default profile — "Bootstrap / Fast-Cashflow":**
- **Maximize:** time-to-first-revenue, gross margin, automation/solo-operability, near-zero upfront capital.
- **Heavily weight (soft):** solo/1-person feasibility, low ongoing founder workload, low support burden.
- **Tolerate:** small market, modest ceiling, weak-ish moat (if fast + cheap to revenue).
- **De-prioritize:** huge TAM, long-term defensibility, market-growth-rate.
- **Hard gates:** upfront capital > $2,000 → kill; time-to-first-revenue > ~4 months → kill; fails any specificity hard-gate (§16) → kill.

**Future profiles** (e.g., "Balanced," "Ambitious / Bigger-Ideas") reuse the same machinery with relaxed gates and re-weighted dimensions. **No rewrite required** — this is the "scalable to bigger ideas" requirement.

### 13.3 Non-compensatory scoring
The framework is **non-compensatory**: a hard-gate breach or a red-team **fatal flaw** forces a **Kill** regardless of how strong other dimensions are. The composite score is used **only to rank** the shortlist, never as the decision itself.

---

## 14. Judge / Evaluation System

Evaluation is **adversarial and two-role** (replacing the single judge):

1. **Red-Team / Critic (Stage 5a)** attacks first (§9.7).
2. **Judge (Stage 5b)** scores the framework **after** seeing both plan and attack.

**Judge output:**
- Per-dimension 1–5 scores, each with written justification.
- Composite score (ranking only).
- **Categorical verdict** — the actual decision payload:

| Verdict | Meaning / operator action |
|---|---|
| **Pursue** | Economics and evidence are strong enough to justify starting now. |
| **Promising-but-unproven** | Could work, but hinges on load-bearing assumptions X/Y. **Go run the named cheap experiments before committing.** (Expected to be the most common verdict, and that is correct.) |
| **Park** | Not now — real but weak, or blocked by a condition. Kept in DB for later. |
| **Kill** | Disqualified. Forced by any hard-gate breach or red-team fatal flaw. Logged with reason. |

**Overrides:** hard gates and fatal flaws force **Kill** regardless of composite (non-compensatory).

---

## 15. Anti-Hallucination / Quality Controls

Enforced mechanically, not by hope:

1. **No lead / no material claim without a real source** (§7). Unsourced facts are auto-demoted to assumptions (§12).
2. **Validation step** programmatically checks: does every `fact` have a retrievable source? Is confidence justified? What fraction of a section rests on low-confidence assumptions? Assumption-heavy sections are flagged and down-weighted by the Judge.
3. **Cross-referencing requirement** — material claims should be corroborated across independent sources; single-source claims carry lower confidence.
4. **Contradiction surfacing** — conflicts between sources/specialists are surfaced, not smoothed.
5. **Red-team disconfirmation** — an agent whose explicit job is to find disconfirming evidence and fatal flaws (§9.7).
6. **Self-challenge requirement** — agents must actively seek disconfirming evidence before concluding (§7).
7. **Confidence + evidence-quality reporting** — surfaced on the 1-page brief ("% of this rests on unverified assumptions").
8. **No-fabrication rule** — "unverified / could not find data" is the required output when evidence is absent; inventing figures is forbidden.

---

## 16. Anti-Generic-Idea Mechanisms

"Generic" is decomposed into **mechanically testable** failure modes, checked at the **triage gate** (Stage 2):

**Hard-kill tests (all three must pass to survive triage):**
1. **Named-customer test** — names a specific, narrow buyer segment (not "small businesses" / "developers").
2. **Specific-pain test** — a *cited*, documented instance of that segment expressing the pain (not hypothesized).
3. **Why-now test** — a specific recent change (not "AI is getting better").

**Scored but not hard-kill:**
4. **Why-hasn't-it-been-solved test** — a concrete reason incumbents haven't done it.
5. **Wedge test** — a specific narrow entry point a solo founder could start with.

**Generic-pattern blocklist:** patterns like "AI chatbot for X," "Uber for X," "productivity app," "app for small businesses" are auto-flagged and must clear a **higher bar** (very strong evidence + specific reason) — not auto-killed, since the operator allows them *with* strong evidence.

**Elegance:** the three hard tests are simply "does this lead carry these three specific structured claims, each with a real source?" — reusing the §12 machinery. Anti-generic is enforced by structure, not vibes. The three tests are **hard kills at triage**, accepting the risk of occasionally killing a real opportunity that was merely described too vaguely.

---

## 17. Financial Reasoning

**Model, don't assert.** For each opportunity taken to Strategy:

- Build a **minimal unit-economics model** (e.g., price × conversion × volume − costs), **anchored** to whatever real data was found (actual competitor pricing, actual tool/ad costs).
- Fill remaining inputs with **clearly-labeled assumptions**, each with stated reasoning (an assumption inside a transparent model is honest; a bare asserted figure is not).
- Run **three scenarios: conservative / base / optimistic.**
- Identify the **load-bearing assumptions** — the 1–3 inputs the outcome is most sensitive to.
- Output ranges and sensitivities, **never** a single asserted "profit = $X."

**Profitability is economics, not TAM.** A $100M market with terrible economics is worse than a $10M niche with excellent economics. The framework weights margin, CAC, LTV, pricing power, sales cycle, churn, capital, time-to-revenue, reachability, founder workload, and automation potential — not market size alone.

**Load-bearing assumptions drive validation.** The most outcome-sensitive assumptions become the **top items in the validation backlog** (§20) — the things the operator should test first and cheapest.

**No fake precision.** Early-stage multi-year projections are forbidden. Estimates are labeled as estimates with their assumptions exposed.

---

## 18. Small-Team Feasibility

A **first-class** part of evaluation (its own dimension, §13.1) and its own dossier section. For each opportunity, the system explicitly answers:

- What work **must humans** do?
- What can **AI** automate?
- What can **software** automate?
- What can be **outsourced**?
- What requires **specialized expertise**?
- What requires **salespeople / customer support / operations / physical presence**?
- What **prevents this from being a one-person business**?
- What is the **minimum viable team** (1 / 2 / 3)?

**Representation:** small-team feasibility is a **heavily weighted soft factor**, not an absolute filter (per the operator's instruction not to let it kill unusually attractive opportunities). The default profile penalizes larger minimum teams heavily but allows a genuinely exceptional 3-person opportunity to survive. The 1-page brief states the **minimum viable team** and the **single biggest threat to solo-operability**.

---

## 19. Iteration & Feedback

### 19.1 Intra-run: bounded kickbacks
Any downstream agent may raise a **blocking gap** ("customer segment undefined — can't assess willingness to pay") that sends the opportunity back **one stage** for one more targeted research pass. **Cap: 1 kickback per stage per opportunity**, logged, with a hard budget stop. Not a free-for-all — a bounded, specific "go get this one missing thing" mechanism.

### 19.2 Cross-run: lightweight preference learning
Every checkpoint **veto** and every verdict **override** prompts a one-line reason ("too much manual sales," "market too small even for me," "loved the automation angle"). These accumulate into a **human-readable operator preference profile** that Discovery and Triage read as **soft steering** on future runs. Same mechanism as the Opportunity Profile, learned incrementally.

**Guardrails against a filter bubble:**
- Learned preferences are **soft steering only, never hard filters.**
- An **exploration allowance**: a small fraction of each run's discovery deliberately **ignores** learned preferences, to keep surfacing opportunities the operator's stated taste would have hidden.

**Full outcome-based closed-loop learning** (tuning weights from real-world results of pursued ideas) is **deferred** — it needs many real outcomes that won't exist for months/years.

---

## 20. Final Output

**Layered deliverable.**

### 20.1 Layer 1 — the 1-page Decision Brief (the thing you read to decide)
Dense, scannable, every element traceable to the dossier:

1. **Verdict + one-line thesis** — the category + one sentence: *"[Specific customer] pays for [X]; a solo founder can capture it because [why now + why small-team-able]."*
2. **The opportunity in 2–3 sentences** — what it is, who pays, what they pay for.
3. **Why now** — the specific recent change, with confidence flag.
4. **Economics snapshot** — base-case unit economics one-liner + the conservative scenario; the 1–3 **load-bearing assumptions** flagged.
5. **Small-team feasibility verdict** — minimum viable team (1/2/3) + the biggest threat to solo-operability.
6. **Competition + why customers switch** — one line each.
7. **The kill risks** — the red-team's top 1–3 "what could kill this," honestly stated.
8. **Cheapest next experiment** — the single highest-leverage, cheapest test for the top load-bearing assumption (rough cost/time).
9. **Confidence + evidence quality** — overall, plus "% of this rests on unverified assumptions."

### 20.2 Layer 2 — the full Dossier (the thing you use to act on a "yes")
Everything at depth, every claim linked to its source (§11.3): full customer/competitor/market research with sourced claims; the complete unit-economics model with all scenarios and assumptions; full GTM and MVP plan; the small-team operating breakdown; the complete red-team attack; per-dimension judge scores with justifications; and the full **ranked validation backlog** (experiments derived from load-bearing assumptions).

### 20.3 Layer 3 — the promotable shortlist (in the DB)
The runners-up from Stage 2.5, ranked by shallow score, each promotable to a full deep-research run later (spending another run's budget). Preserves portfolio value across runs.

**The brief answers "is this worth my time?"; the dossier answers "how do I do it?"; the shortlist answers "what's next?"** Together they let a founder understand: what the opportunity is, why it exists, why now, who pays, what they pay for, how much, who competitors are, why customers switch, how to build the MVP, how to get first customers, what it could cost, how it makes money, what remains unvalidated, what could kill it, and what to test next.

---

## 21. Robustness & Resource Exhaustion

Runs are **stateful and resumable**. State is persisted continuously; nothing important lives only in an LLM context.

### 21.1 Run state machine (nested)
`run → stage → agent → sub-task`, each independently checkpointed. States include: `running`, `awaiting-checkpoint`, `halted-budget`, `halted-error`, `completed`. A partial run is a **first-class, resumable, inspectable object** — never a silent loss.

### 21.2 Context-window overflow inside an agent — **must not stop the run** (operator's top concern)
An agent's LLM context is **disposable working memory**; the source of truth is the persisted record. Overflow is an **expected, recoverable event handled automatically** (no human, no extra spend beyond normal work — it is a memory operation):

- **Every agent's durable state** lives in the DB as three compact, continuously-updated things: (1) the **structured claims** produced so far, (2) a **running state-of-work summary**, (3) a **work ledger** (sub-tasks done vs. pending).
- **Each agent's job is decomposed into small, atomically-committed sub-tasks.** Each sub-task flushes its claims to the DB the instant it completes. At any moment, **at most one in-flight sub-task is unsaved.**
- **Proactive compaction before the hard ceiling.** The agent monitors context usage and, at a threshold, performs a **compaction handoff**: flush claims, update the summary and pending ledger, then **spawn a fresh continuation context rehydrated from that compact state** — not from raw history. The continuation resumes at the next pending sub-task, never re-reading raw pages or redoing completed work.
- **Net effect:** hitting max context **resumes from the exact point it ran out**, losing at most one sub-task's worth of work.

### 21.3 Budget exhaustion ($20) — **stop-and-wait** (a spend decision)
Distinct from context overflow because it is about money, not memory:

- **Budget is decomposed per-stage and pre-checked** before each expensive step. The orchestrator never starts a stage the remaining budget can't afford.
- On exhaustion, the run **stops cleanly at the last completed stage** and **waits** for the operator to either **top up and resume** (from the last checkpoint, reusing persisted work) or **accept the best partial deliverable**.
- **Graceful degradation, never nothing.** A halted run still emits the best partial deliverable — e.g., a 1-page brief marked **"INCOMPLETE — research halted,"** verdict capped at "Promising-but-unproven," with an explicit list of which research axes were never examined. Honesty extends to the run's own completeness.

### 21.4 Other failures
Crashes and rate-limits are handled by the same nested checkpointing: recovery always resumes from the **last committed sub-task**.

---

## 22. Explicitly Open (Left to Implementation)

Intentionally **not** decided here (choose during implementation without violating this intent):

- Programming language, frameworks, runtime, deployment/infrastructure.
- Specific model **vendors** (only tiers — cheap / medium / premium — are specified).
- Specific search/retrieval APIs and specific signal-source websites (only **archetypes** are specified; the set is extensible).
- Database technology (only that persistence is durable, queryable, and supports dedup + nested run state).
- Exact budget/weight/threshold **numbers** (all are tunable config; defaults are given where they express intent).
- The full-knowledge-graph evidence model (§12) and outcome-based learning (§19) — deferred future work.
- UI/presentation specifics of the brief and dossier (only structure and content are specified).

---

## 23. Consistency Check (tensions deliberately resolved)

| Tension | Resolution |
|---|---|
| **Breadth vs. cost** | Breadth bought cheaply (snippet-first, cheap models, parallel discovery); depth spent on **one** opportunity/run (§8.4). |
| **Research depth vs. $20 ceiling** | Three gates before deep research; one deep dive per run; DB dedup reuse (§6.3, §8). |
| **Evidence honesty vs. useful output** | Model-don't-assert economics + labeled assumptions make "unverified" honest *and* useful (§17); output visibly marks confidence. |
| **Profitability vs. small-team** | Both are heavily weighted, non-absolute dimensions under a risk-adjusted profile; profile tilts toward fast-cheap-profit (§13.2, §18). |
| **Avoiding generic vs. breadth** | Anti-generic enforced as *structural* hard-gates reusing the claim model, so breadth stays cheap while junk is killed early (§16). |
| **Small-team strictness vs. missing gems** | Small-team feasibility is a **heavy soft** factor, not a hard filter; capital/time are hard gates because the operator's constraints are real (§6, §18). |
| **Speed/cost vs. quality** | Model tiering + bounded budgets + red-team-before-judge concentrate quality spend where it changes the decision (§8.4, §14). |
| **Robustness vs. cost** | Context overflow auto-recovers (free memory op); only *budget* exhaustion involves the operator (§21). |
