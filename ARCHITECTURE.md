# How the system works — end to end

*A plain-language walkthrough of what actually happens when you run the program, written from the code (not the design docs). For your eyes.*

---

## The one-sentence version

You give it an **industry** (or nothing at all). It goes out and finds **real evidence** of business pain on the internet, filters hundreds of leads down to one, researches that one deeply, writes a concrete business plan, attacks the plan, scores it, and hands you a **1-page decision: Pursue / Promising / Park / Kill** — with every claim linked to a real source. It never invents facts, and it stops the moment it would cross a **$20 spend ceiling**.

---

## The mental model: a funnel, not a pipeline

Think of a funnel that gets narrower and more expensive as you go down. The whole design philosophy is: **buy breadth cheaply at the top, spend depth on one winner at the bottom.**

```
   INDUSTRY (or nothing)
          │
          ▼
  ┌───────────────────┐
  │ 1. DISCOVERY      │  many cheap agents mine the web for leads     ~cheap, wide
  │                   │  (complaints, reviews, job posts, reg/tech
  │                   │   changes, market gaps). Every lead MUST
  │                   │   cite a real URL.
  └───────────────────┘
          │  ~30 leads
          ▼
  ┌───────────────────┐
  │ 2. TRIAGE         │  cheap filter: kill duplicates, kill generic  ~very cheap
  │    (gate)         │  ideas, keep only leads with a named
  │                   │  customer + cited pain + a real "why now".
  └───────────────────┘
          │  fewer leads
          ▼
  ┌───────────────────┐
  │ 3. SHALLOW        │  roughly score the survivors, rank them,       ~cheap
  │    VALIDATION     │  produce a shortlist. Pick the #1 candidate.
  └───────────────────┘
          │  a ranked shortlist
          ▼
  ┌═══════════════════┐
  ║ 4. CHECKPOINT     ║  ⏸  THE RUN PAUSES HERE (default mode).       (you decide)
  ║    (you decide)   ║  You review the shortlist and either
  ║                   ║  approve #1, pick a runner-up, or veto all.
  └═══════════════════┘
          │  one chosen opportunity
          ▼
  ┌───────────────────┐
  │ 5. DEEP RESEARCH  │  FOUR specialists run in parallel on the      $$ expensive
  │                   │  ONE winner: customer/demand, competition,
  │                   │  market/economics, feasibility & why-now.
  └───────────────────┘
          │
          ▼
  ┌───────────────────┐
  │ 6. SYNTHESIS      │  merge the four reports, flag contradictions  $$
  │                   │  between them, assemble the full picture.
  └───────────────────┘
          │
          ▼
  ┌───────────────────┐
  │ 7. STRATEGY       │  turn research into a concrete plan: what to  $$
  │                   │  build (MVP), pricing, go-to-market, a
  │                   │  small-team operating model, scenario
  │                   │  economics, and a validation backlog.
  └───────────────────┘
          │
          ▼
  ┌───────────────────┐
  │ 8. RED-TEAM       │  an adversarial critic ATTACKS the plan and   $$
  │                   │  lists its kill-risks and fatal flaws.
  └───────────────────┘
          │
          ▼
  ┌───────────────────┐
  │ 9. JUDGE          │  scores the opportunity AFTER seeing the      $$
  │   (evaluation)    │  attack. Certain fatal flaws can override a
  │                   │  high score (non-compensatory). Issues the
  │                   │  verdict: Pursue / Promising / Park / Kill.
  └───────────────────┘
          │
          ▼
  ┌───────────────────┐
  │ 10. REPORT        │  FREE (no AI call). Projects everything        $0
  │                   │  stored into a 1-page brief + a full,
  │                   │  evidence-linked dossier + a shortlist.
  └───────────────────┘
```

---

## What each stage does, in detail

### 1. Discovery
Several agents run **in parallel**, one per "signal source." A signal source is a *type* of real-world evidence where business pain shows up:
- **complaints** (people publicly angry about something),
- **reviews** (1-star reviews = unmet needs),
- **job posts** (companies hiring for a pain = they'll pay to solve it),
- **regulatory / technology changes** (a new law or API that just opened a gap),
- **market inefficiencies** (mispricing, manual work that could be automated).

Each agent searches the web (via the Tavily search API), reads pages, and emits **leads**. The iron rule, enforced in code: **every lead must cite a real, retrievable URL.** LLM "I think there's an opportunity in…" is banned as a source. A lead with no source is dropped by a guard.

### 2. Triage (a gate)
Cheap, fast filtering. Three things happen:
- **Dedup** — leads already seen in this or past runs are dropped (there's a cross-run dedup index so you never see the same dead idea twice).
- **Generic hard-kill** — vague "AI for small businesses"-style leads are killed by a blocklist.
- **Specificity test** — a lead survives only if it has all three: a **named customer**, a **cited specific pain**, and a **real why-now**. These are *hard gates*.

### 3. Shallow validation (a gate)
The survivors get a rough score across a few dimensions and are **ranked**. This produces the **shortlist**. The point is cheap triage of *which one* deserves the expensive deep dive — the composite score here is a *ranking device, not a verdict*.

### 4. Checkpoint — **you decide** (the pause point)
By default the run **stops here and waits for you.** You see the ranked shortlist with a one-line rationale for each, and you choose:
- `--approve` → deep-research the #1 candidate,
- `--pick <id> --reason "..."` → deep-research a runner-up instead,
- `--veto --reason "..."` → reject all of them (the run ends here with an honest partial report).

If you run with `--autonomous`, this pause is skipped and it auto-picks #1.

### 5. Deep research
Now, and only now, the system spends real money — on **one** opportunity. **Four specialist agents run in parallel:**
- **Customer / demand** — who exactly pays, and is the demand real?
- **Competition** — who else is doing this, and why would anyone switch?
- **Market / economics** — size, pricing, unit economics.
- **Feasibility & why-now** — can a solo person actually build/run this, and why is now the moment?

### 6. Synthesis
The four reports are merged into one coherent picture, and — importantly — **contradictions between the specialists are flagged**, not smoothed over. If the customer agent says "people will pay $200/mo" and the market agent found the going rate is $20, that conflict is surfaced.

### 7. Strategy
Research becomes an actual **plan**: what to build (the MVP scope and what's explicitly excluded), the revenue model and pricing, distribution/go-to-market, a small-team operating model (who does what, what's automated, what's outsourced), **scenario economics** (conservative / base / upside, each with *labeled assumptions* — never a single asserted number), and a **validation backlog** (the cheap experiments that would prove or kill the assumptions).

### 8. Red-team
An adversarial agent tries to **kill the plan**. It produces attacks, each with a severity, and can name a **fatal flaw**. This happens *before* scoring — you never get a favorable score on something that hasn't been attacked.

### 9. Judge (evaluation)
The judge scores the opportunity across dimensions (evidence quality, assumption risk, small-team fit, economics, etc.) **after reading the red-team's attacks.** Scoring is **non-compensatory**: a fatal flaw can override an otherwise-high score. Output is a **verdict**: `Pursue`, `Promising-but-unproven`, `Park`, or `Kill`.

### 10. Report (free)
This step makes **no AI calls and costs nothing** — it just *projects* everything already stored in the database into three layers:
- **Layer 1 — the 1-page brief:** verdict + thesis, who pays + pricing, why now, base/conservative economics with load-bearing assumptions, minimum viable team, competition + why-switch, top kill-risks, the **single cheapest next experiment** to run, and a confidence/evidence-quality score.
- **Layer 2 — the dossier:** every section in full, with each claim traced back to its source URL and credibility tier, the full attack log, and per-stage cost.
- **Layer 3 — the shortlist:** the runners-up, saved so you can promote one later without re-running discovery.

---

## The cross-cutting rules that make it trustworthy

These run *through* every stage — they're the reason this isn't just another idea generator:

1. **Evidence or it doesn't exist.** No opportunity is real until something was *found* and cited.
2. **Never fabricate.** "Could not verify" is a valid, first-class answer. Invented numbers are forbidden.
3. **Facts vs. assumptions are structural.** Every material claim carries a *type*, a *source*, and a *confidence level* — mechanically, not as a writing style.
4. **Cost is a hard constraint.** A run has a **$20 ceiling** (default). Before each stage, if the remaining budget can't cover that stage's minimum, the run **halts cleanly** instead of starting it — and you get a partial report explaining where it stopped.
5. **Durable state, disposable context.** Everything is written to a database (SQLite) as it happens. If the process crashes, the machine reboots, or the budget runs out mid-run, you can **`resume`** exactly where it left off — nothing is lost, no work is repeated.
6. **Attack before you score.** Always.
7. **A decision, not a number.** The output maps to an action you take, not a score you have to interpret.

---

## What controls a run (the knobs)

You compile your inputs over a **profile** (default: `bootstrap` — favors fast, cheap paths to real profit for a solo founder). The knobs that matter:

- **industry / geography / target-customer** — narrow the hunt (all optional).
- **signal sources** — which evidence types to mine (default: all five).
- **lead target** — roughly how many leads to gather (default 30).
- **budget ceiling** — the hard spend cap (default $20).
- **checkpoint mode** — pause for you (default) or run autonomously to a verdict.
- **hard gates** — max upfront capital (default ≤ $2,000) and time-to-first-revenue, which kill opportunities that don't fit a bootstrapper.

All of these are *parameters*, not hardcoded — the same engine could later hunt "bigger ideas" just by swapping the profile.

---

## Storage — yes, everything is kept

There's a real database (**SQLite**, at `./data/opp.db`). Every run, stage, agent run, lead, opportunity, claim, source, evaluation, experiment, report, kill, and cost trace is stored permanently. You can list a past run's leads, re-print its report, check its cost, resume it, or promote one of its shortlisted runners-up — days or weeks later. See the "database" answer in chat for the full table list.

---

## The commands, in the order you'd use them

```bash
# one-time setup
uv sync
uv run alembic upgrade head        # build the database
cp .env.example .env               # add your Tavily + Anthropic API keys

# start a run (stops at the checkpoint)
uv run opp discover --industry "athletics" --leads 20

# see the shortlist and decide
uv run opp checkpoint <run-id>
uv run opp checkpoint <run-id> --approve

# read the result
uv run opp report <run-id>

# anytime, on any past run:
uv run opp leads <run-id>          # its leads + sources
uv run opp cost <run-id>           # what it spent, by stage/agent
uv run opp resume <run-id>         # continue a halted/crashed run

# or: do the whole thing hands-off
uv run opp discover --industry "athletics" --autonomous
```
