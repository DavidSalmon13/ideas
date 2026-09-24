# Cassettes

Recorded LLM, search and fetch calls, replayed so tests and offline runs never hit a real
API (plan.md §20). Format and modes are documented in `src/opp/cassettes/cassette.py`.

- **Replay** (default): wrap nothing, e.g. `CassetteSearchProvider(Cassette.open(path))`.
  A request with no recording raises `CassetteMissError`; it never goes live.
- **Record**: wrap the real provider, e.g.
  `CassetteSearchProvider(Cassette.open(path, CassetteMode.RECORD), real_provider)`, and
  save (the `with` block does it). `ONCE` records only what's missing.
- Changing an agent's output model changes the LLM request key, so its recordings miss
  and must be re-recorded. That's deliberate.

Cassettes hold only provider-neutral request/response data — never API keys or headers.
Review a new recording before committing it: no personal data, no secrets (plan.md §31).

`example_research.json` was recorded through the real Tavily adapter and Fetcher pointed
at simulated servers, plus a scripted LLM reply, so it has the exact shape a live
recording has.
