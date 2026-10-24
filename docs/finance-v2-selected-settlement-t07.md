# Finance v2 T07 selected repayment candidate

Authority: [frontend issue 45](https://github.com/core-console/front-end/issues/45)
and the unchanged specification at
`e206bdd5af48a3f6b3b9b525c27fa6178220bdc7`, sections 5.4, 5.6 and 7.5.

Settlement command version 4 requires a nonempty, unique `selectedObligationIds`
array. Preview accepts that same draft without the confirmation token. Each ID
must identify an outstanding Obligation belonging to the owned Ledger and chosen
Participant, in the receiving currency, dated no later than the receipt.
Allocation uses the existing oldest-Advance-date order within that set only.
Selection order is immaterial; retained canonical intent sorts the stable UUIDs.

The three existing financial meanings are preserved: omit disposition to repay
only within capacity; explicitly choose `excessDisposition: "retainCredit"` for
receipt-backed Liability; or choose `"acceptIncome"` with one positive, complete
Category Allocation equal to the surplus. There is no scope spill, implicit Income,
forgiveness, credit consumption, or new financial Transaction kind. Commands 1–3
keep their frozen grammar, default version and response shapes.

Confirmation binds selection, disposition, current selected composition and
relevant financial/reference state into the preview token. Scope expansion needs
a fresh preview and a deliberate new confirmation. A changed preview is rejected
before any financial effects. A fully paid selected debt is not silently replaced
with other debt. Recovery retains the original command, scope, key and version;
lookup never executes and same-key retry never duplicates effects.

Migration `20261024_01` adds retained selected UUIDs to confirmed Settlements and
extends PostgreSQL submission guards additively. Selected responses include
`selectedObligationIds`, even when a selected debt received no Allocation. This
retained scope is distinct from actual confirmed Allocation order. Populated
downgrade refuses any version-4 admission or selected Settlement; legacy evidence
remains readable. Linked receivable, credit and Income protections are reused.
Gross debt and credits remain separate Participant projections.

Tests cover the 40/60 selection, 80 receipt against selected 60, explicit expansion,
retained credit with zero net exposure and unpaid gross debt, Income, eligibility,
legacy rejection, stale reconfirmation, PostgreSQL grammar/migration guards and
group rollback/committed-response-loss recovery. The frontend contains an opt-in
real Chromium/TCP test server using this backend's guarded PostgreSQL fixture.
Run it only after other PostgreSQL tests finish; it resets the same dedicated
test target. See the frontend T07 note for commands.

This is an uncommitted producer candidate based on
`1c849355f7c94ff6cb4b53fb5874cf8f8f392e0e`. It supplies local consumer contract
alignment, not a delivered producer P. Committed-P synchronization, both exact-SHA
CI jobs, publication receipts and delivered P+C acceptance remain separate gates.
No issue closure or production activation is implied.
