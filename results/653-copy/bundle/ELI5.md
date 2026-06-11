# ELI5 — red-test mutation gate for phase `copy`

## What the operator asked for
- ACCEPT: every in-spec metric value stated WITH its scope guard is accepted (ok:true), INCLUDING multi-digit-run values like $0.32 whose digit-runs are 0 AND 32
- REJECT: a number that is absent from the spec (fabricated, e.g. a '99 win rate') -> ok:false; an in-spec metric value stated WITHOUT its scope guard (unguarded) -> ok:false

## The three derived versions
- **agent test** (`copy.test.js`): the failing acceptance test, written from the picks.
- **reference** (`copy.js`): the RIGHT version that satisfies every pick.
- **wrong-stub** (`wrong_stub_digit-run-drop.js`): Defeats the ACCEPT pick (multi-digit-run values like $0.32 must be accepted) — by collecting numbers with Set.add(...runs), only the FIRST digit-run per field is kept (0), so the second run (32) is treated as fabricated and the guarded $0.32 example is wrongly rejected (ok:false). This is the operator's discriminating example.
- **wrong-stub** (`wrong_stub_no-fabrication-check.js`): Violates the REJECT pick 'a number absent from the spec (fabricated) -> ok:false' — the fabricated-number loop is removed, so '99 win rate' is no longer flagged and ok comes back true.
- **wrong-stub** (`wrong_stub_no-unguarded-check.js`): Violates the REJECT pick 'an in-spec metric value stated WITHOUT its scope guard (unguarded) -> ok:false' — the unguarded loop is removed, so bare '$0.32' (no scope present) returns ok:true.

## Why the wrong version is wrong (author's words)
We let a writer brag about real numbers, but only if every number was actually measured and is shown next to the 'where this came from' note (the scope). The tricky price $0.32 is really TWO number-chunks: 0 and 32. The right code remembers BOTH chunks as allowed, so when it sees $0.32 in the post it nods 'yep, both 0 and 32 are real, and there's a scope note nearby — fine.' The first wrong version is lazy: when it writes down the allowed numbers it only keeps the FIRST chunk of each field (0) and forgets 32, so later it screams 'where did 32 come from?!' and rejects a perfectly true sentence. The other two wrong versions each switch off one guard: one stops checking for made-up numbers (so a fake '99 win rate' sails through), and the other stops checking for missing scope notes (so a naked '$0.32' with no context passes). The right version keeps all three checks and remembers every digit-chunk.

## What happens next
After you APPROVE, the mutation gate plants each WRONG version (the test MUST go red) and the REFERENCE (the test MUST go green). A test that can't tell them apart is rejected as a fake oracle.
