---
name: next-step
description: Implement the next unchecked step from the plan.md §35 implementation checklist, test it, mark it complete, commit, and stop. Use when the user says "next step", "continue building", or invokes /next-step.
disable-model-invocation: true
---

# Next step

Implement exactly one step of this project, then stop.

1. Read `intent.md`, `specs.md` and `plan.md`. `plan.md` is the build guide.
   `intent.md` decides product questions, and `plan.md` decides technical ones.
2. Open the checklist in `plan.md` §35 ("Final Implementation Checklist"). Find the
   first unchecked item (`- [ ]`). That item is your step. Do only that step.
3. Build it the way `plan.md` describes: tech stack in §2, folder layout in §3,
   conventions in §31, and that phase's acceptance criteria in §24. Look at the
   existing code first so the new code fits in with it.
4. Write tests for the step. Tests must run offline, with no real API calls.
   Make sure linting, type checks and all tests pass.
5. If the docs don't answer a question, pick the simplest option that fits them, add
   a line to `docs/decisions.md` saying what you chose and why, and continue.
6. When the step is done and the tests pass, mark it complete in `plan.md` §35 by
   changing `- [ ]` to `- [x]`. Then commit and push.
   If the step is too big to finish in one go, commit what works, leave the box
   unchecked, and say what is left.
7. Stop. Don't start the next step. Tell the user what you built, how to check it,
   and which step is next.
