# Finance v2 — Personal Receivable & Settlement

Status: approved authoritative product specification; all product decisions and
AC01–AC48 are frozen. Independent review verdict: PASS WITH NON-BLOCKING NOTES.
This approval does not authorize ticket publication or implementation.

This document specifies the first release of Personal Receivable & Settlement
within Finance v2. It does not rename or replace the existing Finance v1 release.
It is an approved documentation-only product specification, not implementation
authorization or a statement that these capabilities exist today.

## 1. Problem Statement

The Ledger owner can already represent personal receivables financially, but
cannot reliably preserve the relationships between a funded purchase or advance,
the people who owe its amounts, repayments, and the remaining debt composition.

In normal personal use, one person repays accumulated debt without identifying
each historical purchase. The amount may be rounded, and the owner may explicitly
accept a difference. Requiring manual allocation for every repayment creates
friction; recording only an aggregate balance loses the explanation of the debt.

The product must make recording easy while preserving individual Obligations,
deterministic settlement, truthful financial effects, and explicit corrections.

### 1.1 Authority and existing foundation

Existing backend-owned vocabulary remains defined by [CONTEXT.md](../CONTEXT.md).
This draft introduces feature-specific concepts without changing that glossary.
The existing [Finance v1 financial rules](finance-v1-product-api.md) and these
decisions remain the foundation:

- [One Local User owns each Finance Ledger](adr/0003-own-finance-ledgers-by-local-user.md).
- [Atomic transfer-aware financial effects](adr/0004-use-an-atomic-transfer-aware-ledger.md).
- [Current Balance derives from Opening Balance and Account Movements](adr/0005-derive-account-relative-balances.md).
- [Each Finance Account has one currency; Money is exact](adr/0006-denominate-each-finance-account-in-one-currency.md).
- [The backend owns OpenAPI](adr/0002-backend-owned-openapi-contract.md).

This draft specifies new product behavior only for this feature. Traceable
correction and protection of linked records add a concrete feature-specific
history requirement; they do not retrospectively impose append-only accounting
on ordinary Finance v1 records. No database shape, endpoint inventory, generated
contract change, new Transaction kind, or frontend visual design is decided here.

The backend owns this product/domain specification. A future frontend interaction
specification may refine navigation and presentation without redefining these
financial meanings. Existing frontend Finance authority continues to govern
unrelated workflows until a separately approved extension exists.

## 2. Solution

### 2.1 Product goals

An active Ledger owner must be able to:

- record money paid entirely for others or a purchase containing an owner share;
- track private external Participants without giving them Ledger access;
- inspect the individual Obligations composing each participant's receivables;
- record a repayment against one participant without manually dividing it;
- optionally restrict repayment to a selected subset of Obligations;
- preview deterministic Allocations and retain the confirmed result;
- distinguish cash payment, forgiveness, accepted surplus, and participant credit;
- explicitly record an unallocated receipt as participant credit, including
  when no eligible outstanding Obligation exists;
- correct incorrect Advances and Settlements with visible linked consequences;
- reconcile participant positions with Finance financial position and reporting.

The feature is owner-maintained personal bookkeeping. A recorded Obligation is
the owner's record of a claim; it does not establish the participant's agreement.

### 2.2 Minimum first release

The release includes private Participants, single-funded Advances, individual
Obligations, participant debt composition, accumulated and partial repayments,
automatic and subset allocation, explicit differences, participant credits,
credit application/return, receipt-backed credit creation without eligible debt,
Participant archiving, and correction history. Historical migration is not
needed: the current system has no production receivable data requiring migration.

The owner decisions in sections 12.1 and 12.2 define the accepted product
boundary. R01 and R02 are resolved; no known product decision remains open.
This specification is approved as the authoritative product document, and
AC01–AC48 are frozen. Technical design, ticket approval, and implementation
remain separate steps; this document does not authorize their execution or
publication.

### 2.3 Non-goals

The release does not manage other people's books, initiate general borrowing,
collect payments, establish shared ownership, or provide full accounting
reconciliation. The complete exclusion list is in section 11.

## 3. User Stories

The actor in these stories is the active owner of the selected Finance Ledger.

1. As a Ledger owner, I want to record a private participant, so that I can track
   receivables without requiring another person to join the application.
2. As a Ledger owner, I want participant attribution to survive a name change, so
   that renaming a person does not move their debt to somebody else.
3. As a Ledger owner, I want to record a pure advance, so that all funded money
   can be attributed to others without becoming my personal Expense.
4. As a Ledger owner, I want to record my share of a shared purchase, so that
   only my consumption contributes to personal Expense.
5. As a Ledger owner, I want to assign explicit amounts to several participants,
   so that each person's Obligation has a clear source.
6. As a Ledger owner, I want to fund a purchase from an Asset or Liability
   Account, so that cash and credit-card purchases have the correct effects.
7. As a Ledger owner, I want to see funding and shares before saving, so that I
   can detect an inconsistent total before recording it.
8. As a Ledger owner, I want to see each participant's outstanding Obligations,
   so that I can explain what they owe without reconstructing notes.
9. As a Ledger owner, I want receivables and credits shown separately by currency,
   so that a net amount does not conceal what either party still owes.
10. As a Ledger owner, I want chronological activity linked to its sources, so
    that I can understand how the current position arose.
11. As a Ledger owner, I want to enter one accumulated repayment, so that I do
    not have to manually allocate it across every purchase.
12. As a Ledger owner, I want to preview oldest-first allocation, so that I know
    exactly which Obligations the payment will resolve.
13. As a Ledger owner, I want to select a subset of Obligations, so that a
    participant's intended repayment scope can be respected.
14. As a Ledger owner, I want a partial repayment to leave visible remainders,
    so that unpaid composition is preserved.
15. As a Ledger owner, I want excess payment within a selection to require a
    choice, so that money is not allocated outside my intended scope.
16. As a Ledger owner, I want to leave a shortfall outstanding, so that receiving
    a rounded amount does not silently forgive the rest.
17. As a Ledger owner, I want to forgive a selected amount explicitly, so that
    a real concession is distinguishable from payment and incorrect data.
18. As a Ledger owner, I want to combine payment with explicit forgiveness, so
    that one real settlement can be recorded without fabricating cash.
19. As a Ledger owner, I want to accept surplus explicitly, so that its Income
    meaning is visible rather than hidden in a cleared balance.
20. As a Ledger owner, I want to retain an overpayment as credit, so that money
    still belonging to a participant remains a Liability.
21. As a Ledger owner, I want to apply credit explicitly to selected debt, so
    that creating an Advance never consumes that credit silently.
22. As a Ledger owner, I want to record returning credit, so that actual money
    returned reduces the correct Liability without creating another Expense.
23. As a Ledger owner, I want to distinguish paid from forgiven amounts, so
    that a resolved purchase does not falsely claim full payment.
24. As a Ledger owner, I want to correct an incorrect Advance after repayment,
    so that the claim and financial effects reflect truthful original facts.
25. As a Ledger owner, I want excess cash released by a correction to become
    visible credit, so that the system never discards money owed back.
26. As a Ledger owner, I want to correct an incorrectly recorded repayment, so
    that cash, Allocations, Obligations, and credits remain consistent.
27. As a Ledger owner, I want to inspect before-and-after correction history,
    so that changed historical facts are not silently overwritten.
28. As a Ledger owner, I want changed previews to require renewed confirmation,
    so that another update cannot silently change my allocation decision.
29. As a Ledger owner, I want confirmed Allocations to survive later backdated
    entries, so that historical settlement intent is not silently recomputed.
30. As a Ledger owner, I want linked records protected from unrelated edit/delete
    actions, so that balances cannot diverge through another Finance entry point.
31. As a Ledger owner, I want to archive a Participant without deleting their
    history, so that attribution and outstanding financial relationships survive.
32. As a Ledger owner, I want to explicitly record an unallocated receipt as
    participant credit, so that it does not silently become Income or require a
    fabricated Obligation.

## 4. Domain Concepts and Relationship Rules

### 4.1 Participant

A Participant is a stable private external-person identity within one Finance
Ledger. It is not a Local User, Ledger owner, shared member, or balancing Finance
Account. The same person recorded in another Ledger is not automatically linked.

A display-name change does not reassign Obligations, Settlements, or credits.
Renaming a Participant and correcting attribution to a different Participant are
different operations. Once Allocations exist, direct Participant reassignment
is prohibited; use an explicit Correction with the affected attribution and
consequences visible. A truthful payment does not move to another person merely
because an Obligation's debtor was wrong.

Participants are archived, not deleted. Archiving preserves stable identity,
historical attribution, Obligations, Settlements, credits, and correction
history. It does not forgive debt, clear credits, or change financial effects.
Archived Participants are not offered for new Advances, while existing debt and
credit remain available for inspection, settlement, return, and Correction.

### 4.2 Advance/shared purchase

An Advance/shared purchase is one funded event with one funding Finance Account,
one currency, a calendar date, an optional explanatory note, the owner's share,
and explicit participant shares. A pure Advance has an owner share of zero.

At creation, the funded total is positive. The owner share is non-negative,
participant shares are positive, and their exact sum equals the funded total.
At least one external participant has a share; an entirely owner-funded purchase
continues to use ordinary Expense bookkeeping. These are consistency conditions,
not an automatic rounding or share-calculation feature.

One Advance creates one Obligation per participant represented in its shares.
The owner's share does not create an Obligation. Funding may use an Asset or
Liability Account; the source Account's Nature determines the financial signs.

### 4.3 Obligation

An Obligation is one participant's attributed principal from one Advance in its
currency. It retains a source relationship, corrected principal, effective cash
payment, effective forgiveness, effective credit application, and outstanding
remainder. Original and corrected facts remain distinguishable in history.

Outstanding principal is corrected principal less effective cash, forgiveness,
and applied credit. It cannot be negative. Historical Allocations superseded by
an explicit correction remain explainable but are not counted again as effective.
Corrected principal may be zero when the original claim was incorrect; retain
that explanation without presenting a corrected-away claim as a cash payment.

An Obligation is unresolved or partially resolved while principal remains.
When none remains, its resolution composition is still visible. Forgiven debt is
resolved, not cash-paid. An Advance can only be presented as fully resolved when
all its participant Obligations have no remaining principal; credit balances are
still shown separately. Any paid claim must be supported by its effective
payment/credit Allocations, not by net participant exposure alone.

### 4.4 Settlement

A Settlement is a resolution event for one Participant in one Ledger and
currency. It can include actual money received and explicit forgiveness or
surplus handling. Standalone forgiveness and credit application require no
fabricated cash receipt. Credit return records actual money moving back.

A cash repayment identifies the actual amount, event date, and receiving Account.
It defaults to all eligible outstanding Obligations for that participant and
currency. Selecting a subset restricts that scope. A Settlement can produce many
Allocations, and an Obligation can receive Allocations from many Settlements.

A receipt can also be explicitly recorded as participant credit when no eligible
debt exists. That Settlement has no Obligation Allocation for its unallocated
amount; it records the actual receipt and its corresponding credit Liability.
No placeholder Obligation or implicit Income is created.

The final confirmed Allocation result is retained. Automatic allocation is a
decision at confirmation time, not a continuously recomputed historical FIFO.

### 4.5 Allocation

An Allocation records how much of a Settlement resolves a particular Obligation
and whether the effect is cash payment, forgiveness, or credit application.
These meanings cannot be collapsed into an undifferentiated paid amount.

Allocations must respect the Participant, Ledger, currency, eligible date, and
confirmed scope. Effective Allocations cannot exceed corrected principal or
reuse the same cash or credit amount. Surplus not applied to an Obligation has
an explicit credit or Income disposition instead of a fictitious Allocation.

### 4.6 Participant credit

A Participant credit is a Liability owed back to that participant. It arises
from explicitly retained settlement surplus, an explicitly recorded unallocated
receipt, release of excess cash during Correction, or restoration of an existing
credit application during Correction. An unallocated receipt may create credit
without eligible debt; its actual funding must still be recorded. The feature
does not introduce general loan management or unbacked balance creation.

Available credit reflects its recorded origins, applications, actual returns,
and explicit corrections. Application and return cannot use the same available
amount twice. Existing credits are not consumed when a new Advance is created.

Receivables and credits remain separately visible even for the same participant
and currency. Net exposure is receivables less available credits; it is an
additional summary, not automatic netting. A Correction must expose the affected
credit origins, applications, returns, and released/restored amounts. Consumed or
returned credits cannot disappear implicitly or be reset as though their
lifecycle never happened. Actual cash returns remain recorded; an inconsistent
correction cannot be confirmed until its credit consequences are explicitly
resolved. Credit application and return consume available amounts oldest origin
first, using the originating receipt's immutable confirmation order and stable
tie-breakers. Released or restored credit retains its actual originating receipt
and ordering; it is not silently treated as a new source. Section 7.6 defines
the accepted selection rules.

### 4.7 Correction history

A Correction fixes incorrect recorded facts, retains the original entry and
reason for change, and exposes all affected effective records and financial
consequences. It does not record later renegotiation, a merchant refund, or a
new decision to forgive debt as if the original entry had been wrong.

Correction history and actual financial event dates serve different purposes.
The original financial facts are corrected at their appropriate original dates;
the later correction remains visible as a correction. This does not require
every ordinary Finance Transaction to become an append-only audit record.

## 5. User Workflows

### 5.1 Record an Advance

The owner chooses the Ledger, funding Account, date, total, owner share, and
participant amounts. Currency follows the funding Account. Participants can be
identified privately without invitations. The preview shows funding, personal
Expense, and each receivable. Inconsistent shares cannot be saved.

Confirmation records the complete Advance, Obligations, and financial effect
together. A failure cannot leave only some participants funded or a financial
effect without its required relationships. The UI reports the confirmed result
without requiring the owner to separately record the same Expense or transfer.

### 5.2 Inspect participant debt composition

For each currency, show total outstanding receivables, available credits, and
net exposure. List the constituent Obligations with their source date, Advance,
corrected principal, cash-paid, forgiven, credit-applied, and remaining amounts.
Chronological activity links Advances, Settlements, credit activity, and
Corrections. A corrected entry remains distinguishable from an unchanged one.

Do not hide Obligations merely because unapplied credit makes net exposure zero.
An unpaid purchase remains unpaid until effective Allocations prove resolution.

### 5.3 Record repayment and automatic allocation

The owner enters the Participant, receiving Account, actual receipt date, and
exact cash amount. Currency must match the affected Obligations and Account.
The default scope contains eligible outstanding Obligations for that Participant
and currency, where the Advance date is on or before the Settlement date.

Allocate cash oldest Advance date first, with a stable non-editable tie-breaker
for same-date Obligations. The preview exposes each amount applied and remaining,
the total receipt, and any unresolved difference. The same unchanged state and
inputs produce the same proposed allocation. The precise stable key is not an
owner-facing configuration option.

Confirmation retains the actual Allocation result. Later backdated Advances or
changed presentation sorting do not redistribute that result. Future-dated
entries follow Finance v1 date semantics: they are already-recorded financial
events affecting Current Balance immediately, not scheduled instructions. A
future or backdated change cannot silently rewrite confirmed Allocations.

### 5.4 Selected subset allocation

The owner may explicitly select eligible outstanding Obligations. Allocation
uses the same deterministic rule within that set only. If cash exceeds selected
capacity, confirmation requires an explicit choice to expand scope, retain
excess as credit, or accept excess as surplus with its Income effect visible.

The system cannot silently spill into unselected Obligations. If unselected
debt remains, it stays visible even when the owner retains credit or accepts
surplus. An expanded selection requires an updated allocation preview.

### 5.5 Shortfall and forgiveness

Receiving less than the selected debt leaves the remainder outstanding unless
the owner explicitly forgives an amount. There is no rounding threshold that
automatically clears the difference.

When payment and forgiveness are combined, allocate actual cash first, then
forgiveness against the remaining selected capacity. Forgiveness cannot exceed
that remainder. A standalone forgiveness action selects debt, an exact amount,
an actual forgiveness date, and a reason, with its Expense effect previewed.

History distinguishes cash payment from forgiveness, including when both occur
in one Settlement. No zero cash receipt is invented to explain forgiveness.

### 5.6 Surplus handling

Actual excess has two different meanings: accepted surplus belongs to the
owner and contributes to Income; retained credit still belongs to the Participant
and contributes to Liabilities. The owner explicitly chooses the disposition
and sees amounts and financial effects before confirmation. No excess is lost
by clamping an Obligation or participant balance to zero.

With no eligible outstanding Obligation, the owner can explicitly choose to
record the unallocated receipt as participant credit. Preview the actual receipt
and matching credit Liability before confirmation. Do not silently create
Income, invent an Obligation, reverse earlier forgiveness, or apply the receipt
to ineligible or unselected debt. Accepted surplus remains a separate explicit
choice with its Income meaning visible; receipt-backed credit is not a general
borrowing-management workflow.

### 5.7 Apply or return credit

Applying credit requires an explicit action, amount, eligible target Obligations,
and preview. It uses deterministic allocation within the confirmed scope, reduces
available credit and receivables equally, and creates no fresh cash receipt,
Income, or Expense. A new Advance never triggers this action silently.

Returning credit records the actual amount, date, and paying Account. It reduces
available credit and the corresponding Liability, with the matching Account
effect, without inventing a new Expense. No return or application can exceed
available credit. Application and return consume oldest credit origins first,
according to the originating receipt's immutable confirmation order and stable
tie-breakers. Show the selected origins, amounts consumed, and remaining credit
in the preview and history. Releasing or restoring credit retains its provenance.

### 5.8 Correct a record

The owner identifies incorrect facts, supplies corrected facts and a reason,
and reviews before/after results. The preview distinguishes corrected original
funding, owner Expense, participant principal, actual receipts, Allocations,
forgiveness, and released/restored credits. Confirmation applies the complete
consistent result and preserves correction history.

Correction of an Advance and correction of a Settlement are both required.
The accepted correction and cancellation rules are in sections 7 and 12.

## 6. Financial Rules and Accounting Invariants

### 6.1 Financial meanings

| Event                             | Required financial meaning                                                                                                                     |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Owner share of a purchase         | Personal Expense; funded from the selected Account.                                                                                            |
| Participant share or pure advance | Receivable Asset; principal is not personal Expense.                                                                                           |
| Repayment principal               | Receiving Account effect and equal receivable reduction; no Income.                                                                            |
| Forgiveness                       | Receivable reduction and explicit Expense; no cash receipt.                                                                                    |
| Accepted surplus                  | Income for the actual amount explicitly accepted.                                                                                              |
| Retained overpayment              | Participant credit Liability; no Income.                                                                                                       |
| Explicit unallocated receipt      | Actual receiving Account effect and equal participant credit Liability, even without eligible debt; no implicit Income or invented Obligation. |
| Credit application                | Equal reduction of receivable and credit Liability; no new cash, Income, or Expense.                                                           |
| Credit return                     | Paying Account effect and credit Liability reduction; no new Expense.                                                                          |
| Incorrect original facts          | Restate the affected financial facts, preserving correction history.                                                                           |

Expense and Income classification remains distinct from participant Allocation.
Existing Category Allocations classify economic purpose; they do not identify
people or allocate settlement. Existing exact Money and Category rules remain
applicable without introducing full split-category editing or new defaults.

Credits must contribute to Liability totals, not merely be hidden as negative
receivable Assets. Underlying Finance Account provisioning and management are
not prescribed, but cannot change these gross reporting meanings. Participant
records do not become external balancing accounts for all Income and Expense.

### 6.2 No double counting or competing balance authority

For each Ledger and currency, tracked outstanding Obligations reconcile with
their corresponding receivable positions, and available credits reconcile with
their corresponding Liability positions. Unrelated manual bookkeeping must not
be silently attributed to Participants or counted again as tracked debt.

At receipt recording, actual cash is completely explained by cash applied to
Obligations, retained participant credit, and explicitly accepted surplus.
Forgiveness and use of existing credit are separate non-cash effects. Subsequent
Corrections preserve an explanation of reclassification; actual credit returns
remain separately recorded outflows.

Balances are derived from effective recorded facts and relationships. No free
balance override may introduce an unexplained receivable, credit, gain, or loss.
All linked effects change together. Repeated submission or response recovery
must not record the same confirmed action twice; the eventual delivery protocol
is separate contract work, not specified here.

### 6.3 Worked financial examples

All examples use one currency and Asset cash unless a card is specified.

| Scenario                                                  | Observable outcome                                                                                                                       |
| --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Pay 300; owner share 100; Alex 120; Blair 80              | Funding falls 300; Expense is 100; receivables are Alex 120 and Blair 80.                                                                |
| Same purchase on a credit card                            | Card Liability increases 300; receivables increase 200; Expense is 100. Card bill repayment remains distinct from participant repayment. |
| Alex repays 50 of 120                                     | Cash increases 50; Alex's receivable becomes 70; Income is unchanged.                                                                    |
| Debt 100; receive 98; no forgiveness                      | Cash increases 98; receivable 2 remains visible.                                                                                         |
| Debt 100; receive 98; explicitly forgive 2                | Cash increases 98; receivable becomes zero; Expense increases 2; history shows 98 paid and 2 forgiven.                                   |
| Debt 100; receive 102; accept surplus 2                   | Cash increases 102; receivable becomes zero; Income increases 2.                                                                         |
| Debt 100; receive 102; retain credit 2                    | Cash increases 102; receivable becomes zero; Liability credit is 2; Income is unchanged.                                                 |
| Apply credit 2 to a later Obligation of 10                | Receivable becomes 8; credit becomes zero; cash and Income/Expense do not change.                                                        |
| Return available credit 2                                 | Cash and participant credit Liability each fall 2; Expense is unchanged.                                                                 |
| No eligible debt; receive 50 and explicitly retain credit | Cash increases 50; credit Liability increases 50; Income is unchanged; no Obligation is invented and prior forgiveness is unchanged.     |

## 7. Lifecycle and Correction Rules

### 7.1 Draft discard, create, correct, and cancel

Creating an Advance or Settlement must record its complete consistent domain
and financial result, or none of it. Creation cannot consume existing credits
without the separately explicit credit-application action.

An unconfirmed draft may be discarded without financial effects. Confirmed
Advances and Settlements are never hard-deleted, even when no repayment,
Allocation, forgiveness, or credit activity has followed. An incorrect confirmed
entry uses traceable Correction or cancellation instead, retaining its identity,
original recorded facts, and the complete correction history.

An edit of financial facts is a coordinated Correction, including before any
settlement when it affects already recorded financial effects. A metadata rename
does not change attribution or replay allocations. Existing Account/currency and
ownership rules cannot be bypassed through this feature.

Cancellation is a Correction of an incorrect entry, such as a duplicate; it is
not a merchant refund, new forgiveness, credit return, or evidence that real
cash never moved. Correcting or cancelling an incorrect representation may
remove its false financial effect, but cannot erase a truthful receipt, return,
or other actual money event. A truthful funded event with an incorrect share
uses share Correction rather than pretending the funding did not happen.

Show the complete consequences before confirmation. Repayments, Allocations,
forgiveness, credit origins, applications, and returns remain linked and
explainable. Cancellation after such activity requires explicit handling of
every affected consequence; it cannot silently reallocate payment, erase
consumed/returned credit, or leave an orphan. Block confirmation when a complete
consistent result cannot be established. Participants are archived, not deleted.

| State                                        | Allowed action                                                                                                               |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Unconfirmed draft                            | Discard without creating or removing financial effects.                                                                      |
| Confirmed, with no subsequent activity       | Traceable Correction or cancellation of an incorrect entry; no hard deletion.                                                |
| Repayment, Allocation, or forgiveness exists | Coordinated Correction/cancellation with complete before/after consequences; truthful money and history remain preserved.    |
| Credit has been applied or returned          | Explicit handling of its full lifecycle and linked consequences; block confirmation if the result cannot be made consistent. |

For example, if an Advance of 100 was accidentally recorded twice, cancel the
duplicate through Correction while retaining its history. If a real repayment
of 60 was allocated to that duplicate, do not erase the receipt. Explicitly
resolve its Allocation and any released credit; do not automatically apply the
60 to the other Advance. Any later credit application is an explicit action.

Ordinary Finance Transaction edit/delete paths must not independently mutate
linked effects and leave feature records inconsistent. A linked operation must
use the coordinated workflow or explain why it cannot proceed. This protection
does not change edit/delete rules for unrelated ordinary Finance v1 bookkeeping.

### 7.2 Correct an Advance after settlement

A correction must identify what was incorrect. Reducing a participant share
while retaining the actual funded total increases the owner's share or changes
another explicitly corrected participant share; it does not mean the original
payment was smaller. Correcting the actual funding amount is a different repair.
A later merchant refund or renegotiation is not an original-fact correction.

Use the owner's conceptual correction order:

1. Correct the original recorded facts, including the affected funding and shares.
2. Update affected effective Allocations and recompute the unpaid remainder,
   preserving actual money flows and making attribution/eligibility changes explicit.
3. Adjust forgiveness to the corrected facts so it does not over-resolve principal.
4. Adjust released, restored, or created credits, preserving their explicit lifecycle.

These are conceptual evaluation stages of one consistent Correction, not
independently saved intermediate states. An unpaid remainder absorbs a principal
reduction without inventing a repayment. Forgiveness is adjusted before treating
residual excess cash as participant credit. Existing credit applications or
returns must remain visible with their actual consequences; they cannot be erased
or silently netted away. Within each applicable stage, release newest affected
Allocations first; within one Settlement, reverse its original confirmed
allocation order. Section 7.6 defines the accepted scope and tie-breaking rules.

Make each consequence explicit and retain its relationship to the prior history.
An effective Allocation cannot remain over the corrected principal merely to
avoid updating its history. Do not redistribute released amounts to unrelated
Obligations. Increasing principal creates additional outstanding debt without
silently consuming existing credits or rewriting earlier Settlements.

In the first two examples, funded total 200 remains unchanged and Alex originally
owed the whole amount. The third example has its own funding amount:

- Alex paid 80; correct principal to 180: retain payment 80, leave debt 100,
  and restate owner purchase Expense to 20.
- Alex paid 80; correct principal to 70: retain actual receipt 80, show effective
  cash applied 70 and credit Liability 10, and restate owner Expense to 130.
- Principal 100, cash paid 60, forgiven 40; correct principal to 80 with original
  funding 100: effective forgiveness becomes 20, owner purchase Expense becomes
  20, cash stays 60, and no participant credit is fabricated. Restate the
  affected original purchase and forgiveness facts at their respective dates.

Consumed or returned credit requires an explicit, complete correction plan:
identify its origin, affected applications and returns, any reopened debt or
restored credit, and the resulting financial position. Preserve actual receipts
and returns, before/after history, and non-negative available credit. The owner
must resolve all linked consequences before confirmation; no credit may disappear
implicitly to make the Correction balance. This does not authorize an invisible
cascade into unrelated Settlements or an unexplained gain, loss, or new loan.

### 7.3 Correct a Settlement

Support correction of an incorrectly recorded repayment amount, date, or
receiving Account. Preserve actual cash, not an incorrect entered number. Update
affected effective Allocations, outstanding principal, credits, accepted surplus
classification, and financial effects explicitly as applicable.

A smaller corrected receipt cannot silently increase forgiveness to keep debt
closed. A larger corrected receipt cannot silently become Income or credit:
show its required disposition. Previously confirmed scope is not silently
expanded, and unrelated confirmed Settlements are not automatically replayed.

Preserve before/after history and the explanation for changes. Once Allocations
exist, neither an Obligation nor a Settlement permits direct Participant
reassignment. Correct attribution through an explicit Correction identifying the
old and corrected attribution and all affected Allocations and credits. Do not
move a truthful cash receipt merely because the recorded debtor was wrong.
Consumed/returned credit and invalid-date consequences must be resolved within
the complete previewed Correction, not by silently redistributing other records.

### 7.4 Event dates and confirmed stability

Corrections affect original financial facts at their respective original dates;
correction history separately reveals that those facts were changed. New cash
payment, forgiveness, accepted surplus, credit return, and credit application
are new events with their actual event dates.

An Obligation is eligible for allocation only when its Advance date is on or
before the Settlement date. A stable same-date tie-breaker cannot depend on
editable names or current list presentation. Later backdated entries never
silently redistribute confirmed Allocations. Changing a date cannot silently
leave an invalid effective Allocation. Require explicit correction handling of
the affected links, attribution, and credit consequences before confirmation;
never automatically redistribute them or replay historical FIFO.

Follow existing Finance v1 date semantics: calendar dates have no timestamp or
timezone meaning, future dates are allowed, and an already-recorded future-dated
financial event participates in Current Balance immediately. There is no
scheduled or delayed application. This applies alongside existing Account
Tracking Start Date constraints and the Advance/Settlement eligibility rule.

This document does not promise formal as-of accounting snapshots, historical
close periods, or retroactive replay of all events into a new chronological FIFO.

### 7.5 Stale previews and changed references

If relevant balances, credits, selected Obligations, financial facts, or resource
eligibility change before confirmation, do not silently save a different result
or partially save the old one. Preserve valid user-entered intent, show the
current conflict and refreshed consequences, and require renewed confirmation.

The refreshed preview must not silently expand a subset, consume credit, add
forgiveness, or accept surplus. A failed or uncertain response must not encourage
recording a second financial action without resolving the original outcome.

### 7.6 Deterministic correction and credit-origin selection

Keep the conceptual correction stages in section 7.2: original facts, affected
Allocations, forgiveness, and released/restored/created credits. The following
rules select among eligible records within those stages; they do not reorder
the stages or override preservation of actual cash and forgiveness semantics.

1. Release newest affected Allocations first, using immutable confirmation order
   and stable tie-breakers. Only records actually affected by the Correction
   qualify; do not select unrelated historical Settlements simply because they
   are newer.
2. Within one Settlement, release in reverse of its original confirmed
   allocation order. Retain that order when financial dates or other facts are
   corrected; do not reconstruct it from today's edited dates or list sorting.
3. Apply or return available credit oldest origin first, using the originating
   receipt's immutable confirmation order and stable tie-breakers. This does
   not authorize automatic credit consumption when creating an Advance.
4. Released or restored credit preserves its actual originating receipt and
   lifecycle. A restoration does not become an unexplained new source or change
   that origin's ordering. Preserve actual credit returns and all links needed
   to explain the correction consequences.

Confirmation order is independent of editable financial dates. The chosen
stable tie-breakers cannot depend on names, current presentation, or later date
edits. Ordinary repayment continues to allocate by oldest Advance date first;
its existing confirmed results are not retrospectively rerun by these rules.

For example:

- An Obligation of 90 received 50 and then 40. Correcting principal to 60
  releases 30 from the newer affected payment Allocation: effective payment
  remains 50 plus 10, and credit 30 retains the second receipt as its origin.
  Actual receipts of 90 remain recorded, and source-share financial corrections
  still apply as required by section 7.2.
- One Settlement allocated 40 to an older Obligation and 60 to a newer one.
  Correcting its receipt from 100 to 70 releases 30 from its last Allocation,
  leaving effective applications of 40 and 30. The false receipt amount is
  corrected; no real cash return is fabricated.
- Credits of 20 and then 30 from two confirmed receipts are available. Applying
  or returning 25 consumes 20 from the older origin and 5 from the newer one,
  leaving 25 on the newer origin, with the actual action and provenance visible.

Preview the complete Correction or credit-consumption result before
confirmation. Do not redistribute unrelated historical Settlements or hide
unresolved consequences behind a changed aggregate balance.

## 8. Implementation Decisions

This section records only the implementation boundaries established by product
decisions; it is not an implementation plan.

- Add first-class domain relationships around Advances, Obligations, Settlements,
  Allocations, credits, and Corrections, coordinated with Finance financial facts.
- Retain backend authority for ownership, exact Money, financial effects,
  consistency, and the eventual public contract. A frontend preview cannot be
  a second authority for balances or permission to mutate changed state.
- Preserve the existing atomic transfer-aware foundation. Multiple financial
  effects required by one user action must not become independently mutable
  partial results.
- No new Finance Transaction kind is approved. Choose financial representation
  only after proving it preserves the required effects and existing contracts.
- No storage schema, API fields/paths, endpoint count, technical allocation key,
  module structure, Account provisioning UI, route design, or locale expansion
  is selected by this document.
- Ticket breakdown and technical design must preserve accepted owner policy;
  any proposed scope or policy change requires separate owner approval.

## 9. Testing Decisions and Acceptance Criteria

### 9.1 Accepted behavioral verification boundaries

The primary acceptance boundary is the owner's complete Finance workflow and
its publicly observable results: debt composition, history, Account balances,
financial position, and Income/Expense. Public backend behavior provides a
complementary boundary for consistency, ownership, and rejection of stale or
invalid actions. The owner accepted these behavioral verification boundaries.
They are not an instruction to implement tests in this documentation task.

Prefer existing public Finance API and routed UI verification over assertions
about internal helper calls, tables, keys, or component structure. Existing
Finance API behavior tests, protected transaction/Overview integration tests,
route-level transaction tests, and browser Finance flows provide prior art.
Do not invent a second cache or client only to create a new test boundary.

Future browser coverage should use accessible controls, exercise keyboard/focus
and pending/conflict recovery, and include applicable automated WCAG checks.
Passing automation alone does not establish complete accessibility conformance.

### 9.2 Observable acceptance criteria

All monetary examples are within one Ledger and currency unless stated otherwise.
Criteria associated with D01–D07 and R01–R02 express accepted owner policies.
No acceptance criterion remains pending a product decision. These are required
future observable behaviors, not claims that implementation or tests exist.

| ID   | Observable acceptance criterion                                                                                                                                                                                                                                                   |
| ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| AC01 | Recording a pure Advance creates receivables for its participants and no owner purchase Expense.                                                                                                                                                                                  |
| AC02 | A 300 purchase with owner 100, Alex 120, and Blair 80 shows Expense 100, receivables 200, and the correct total funding effect.                                                                                                                                                   |
| AC03 | The same purchase funded from a credit card increases its Liability by 300 without changing participant shares or reporting participant principal as Expense.                                                                                                                     |
| AC04 | Inconsistent shares cannot be confirmed; no partial financial or participant result remains.                                                                                                                                                                                      |
| AC05 | A Participant can be tracked without another Local User, invitation, debt acknowledgement, or Ledger access.                                                                                                                                                                      |
| AC06 | Renaming a Participant preserves their existing Obligations, Settlements, credits, and attribution.                                                                                                                                                                               |
| AC07 | The participant view shows each Obligation's source, principal, cash-paid, forgiven, credit-applied, and remaining amounts.                                                                                                                                                       |
| AC08 | Receivables and credits remain separately visible by currency, including when net exposure is zero.                                                                                                                                                                               |
| AC09 | Other Ledgers' participants, Obligations, credits, and financial records cannot be viewed or affected through this Ledger, even when owned by the same Local User.                                                                                                                |
| AC10 | An unchanged repayment preview always proposes the same oldest-Advance-date allocation, including stable ordering for same-date Obligations.                                                                                                                                      |
| AC11 | With outstanding Obligations of 40 then 60, a default repayment of 50 applies 40 and 10, leaving only 50 on the second.                                                                                                                                                           |
| AC12 | Selecting only the second of those Obligations prevents allocation to the first.                                                                                                                                                                                                  |
| AC13 | Paying 80 against a selected capacity of 60 requires explicit expansion, credit, or accepted-surplus disposition for 20; unselected debt is not silently paid.                                                                                                                    |
| AC14 | Receiving 98 against debt 100 leaves debt 2 without any automatic rounding or forgiveness.                                                                                                                                                                                        |
| AC15 | Explicitly receiving 98 and forgiving 2 clears debt 100, reports cash 98 and Expense 2, and distinguishes paid from forgiven.                                                                                                                                                     |
| AC16 | With Obligations of 40 then 60, receiving 90 and forgiving 10 applies cash 40 and 50 before forgiveness 10 on the second.                                                                                                                                                         |
| AC17 | Standalone forgiveness reduces selected receivables and reports its Expense without showing a fabricated cash receipt.                                                                                                                                                            |
| AC18 | Receiving 102 against debt 100 and accepting surplus 2 increases Income by 2 and creates no credit for that amount.                                                                                                                                                               |
| AC19 | Retaining that same excess 2 creates a credit Liability, leaves Income unchanged, and preserves the receipt's full amount.                                                                                                                                                        |
| AC20 | Creating another Advance while credit exists leaves that credit unapplied.                                                                                                                                                                                                        |
| AC21 | Explicitly applying credit 2 to debt 10 leaves debt 8 and credit zero, with no new cash, Income, or Expense.                                                                                                                                                                      |
| AC22 | Returning available credit 2 records the actual Account outflow and Liability reduction without Expense; the same credit is no longer available to apply.                                                                                                                         |
| AC23 | A repayment's principal does not increase Income, and a card bill repayment does not settle participant debt without the corresponding participant event.                                                                                                                         |
| AC24 | Cross-currency or cross-participant allocation cannot be confirmed, and no cross-currency total is presented.                                                                                                                                                                     |
| AC25 | An Obligation is not presented as paid merely because forgiveness or unapplied credits make a participant's net exposure zero.                                                                                                                                                    |
| AC26 | A later backdated Advance does not change previously confirmed Allocations.                                                                                                                                                                                                       |
| AC27 | A changed balance or selected Obligation after preview requires refreshed consequences and renewed confirmation; a different allocation is not silently saved.                                                                                                                    |
| AC28 | Correcting a fully funded principal 200 to 180 after cash repayment 80 retains cash 80 and leaves debt 100, with funding/share changes explained.                                                                                                                                 |
| AC29 | Correcting that principal to 70 retains cash 80, shows effective application 70 and credit 10, and never hides the excess by clamping a balance.                                                                                                                                  |
| AC30 | Correcting principal 100 with cash 60 and forgiveness 40 to principal 80 adjusts forgiveness to 20 before creating any cash credit.                                                                                                                                               |
| AC31 | Correcting an incorrect receipt of 80 to an actual 70 updates the financial receipt and linked effective state; it does not automatically forgive the resulting difference.                                                                                                       |
| AC32 | Corrections restate original affected financial facts while preserving before/after correction history; new real events retain their actual dates.                                                                                                                                |
| AC33 | An ordinary Finance edit/delete action cannot leave a linked Advance, Obligation, Settlement, credit, or financial effect inconsistent.                                                                                                                                           |
| AC34 | A failed or retried confirmation cannot leave a partial action or duplicate the same recorded financial event.                                                                                                                                                                    |
| AC35 | Chronological activity distinguishes original facts, confirmed settlement composition, and later Corrections without claiming historical Allocations were unchanged when explicitly corrected.                                                                                    |
| AC36 | A Correction affecting consumed or returned credit explicitly shows its origins, applications, returns, and financial consequences; actual receipts/returns remain recorded and no credit disappears implicitly. Incomplete consequences cannot be confirmed.                     |
| AC37 | Once Allocations exist, direct Participant reassignment is prohibited. Explicit Correction preserves original and corrected attribution, affected Allocations and credits, and truthful cash movements; renaming alone never reassigns debt.                                      |
| AC38 | A date change invalidating Allocation eligibility requires explicit correction handling before confirmation; it neither leaves invalid effective state nor automatically redistributes confirmed Allocations.                                                                     |
| AC39 | Participants cannot be deleted. Archiving retains stable historical attribution, Obligations, Settlements, credits, and correction history, and leaves existing debt/credit available for resolution without changing balances.                                                   |
| AC40 | A Correction evaluates corrected original facts, affected Allocations, forgiveness, and released/restored/created credits in that conceptual order, confirms one consistent result, and exposes all linked consequences.                                                          |
| AC41 | Future-dated entries follow Finance v1: their recorded financial effects participate in Current Balance immediately. Future or backdated changes do not silently rewrite confirmed Allocations, and Allocation date eligibility still applies.                                    |
| AC42 | With no eligible debt, explicitly recording a receipt of 50 as credit increases the receiving Account by 50 and participant credit Liability by 50, creates no Income or fictitious Obligation, and does not reverse prior forgiveness.                                           |
| AC43 | Unconfirmed drafts may be discarded without financial effects. Confirmed Advances and Settlements, including those without subsequent activity, cannot be hard-deleted; incorrect entries use traceable Correction or cancellation.                                               |
| AC44 | Within the applicable correction stage, release newest affected Allocations first by immutable confirmation order and stable tie-breakers; correcting principal 90 to 60 after receipts 50 then 40 leaves effective applications 50 and 10 and credit 30 from the second receipt. |
| AC45 | Within one Settlement originally allocated 40 then 60, correcting its receipt from 100 to 70 releases from its last confirmed Allocation first, leaving applications 40 and 30 without redistributing unrelated Settlements.                                                      |
| AC46 | Applying or returning 25 from available credit origins of 20 then 30 consumes the older origin 20 and newer origin 5, leaving newer-origin credit 25 with the actual action and provenance visible.                                                                               |
| AC47 | Released or restored credit retains its originating receipt and immutable ordering. Changing editable financial dates or restoring credit neither creates a fictitious new source nor rewrites confirmed Allocation history.                                                      |
| AC48 | Correction or cancellation previews all linked cash, Allocation, forgiveness, and consumed/returned credit consequences. If a consistent result cannot be established, confirmation is blocked; truthful cash, history, and credits are not silently erased or reallocated.       |

## 10. Review Against the Grilling Boundary

This section is the document's self-review against the agreed conversation and
subsequent owner decisions. It does not substitute for independent review,
implementation approval, or evidence that the feature is implemented.

| Agreed boundary                                                                   | Draft coverage                                                   | Review result                                                                                                                                                                                |
| --------------------------------------------------------------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Outbound personal receivables, correct owner Expense, private Ledger participants | Sections 2, 4, 6; AC01–AC09                                      | Preserved. Explicit unallocated receipts may also create participant credit under D07.                                                                                                       |
| One funding Account per Advance and one currency                                  | Sections 4.2, 5.1, 11; AC02–AC04, AC24                           | Preserved; mixed funding, multiple payers, and FX remain excluded.                                                                                                                           |
| Debt composition and low-friction accumulated repayment                           | Sections 4.3–4.5, 5.2–5.4; AC07, AC10–AC13                       | Preserved through confirmed Allocations rather than aggregate-only tracking or mandatory manual division.                                                                                    |
| Ordinary repayment allocation, selected scope, and confirmed stability            | Sections 5.3–5.4, 7.4–7.6; AC10–AC13, AC26–AC27                  | Repayment remains oldest-Advance-date-first. Newest-first release applies only to affected correction records; no unrelated historical redistribution.                                       |
| Cash before forgiveness and explicit shortfall/surplus meanings                   | Sections 5.5–5.7, 6; AC14–AC22                                   | Preserved. Stage-local selection does not override the conceptual stages, actual cash preservation, or forgiveness rules.                                                                    |
| Separate gross receivables and credits, with explicit credit lifecycle            | Sections 4.6, 5.7, 6, 7.2, 7.6; AC08, AC19–AC22, AC36, AC46–AC48 | Preserved. Application/return uses oldest origin first; released/restored credits retain provenance; consumed/returned credits cannot disappear.                                             |
| Explicit credit creation for unallocated receipts without eligible debt           | Sections 2, 4.4–4.6, 5.6, 6; AC42                                | Preserved. No silent Income, fabricated debt, or general loan-management workflow.                                                                                                           |
| Original-fact corrections and new actual-date events                              | Section 7; AC28–AC32, AC47                                       | Preserved. Financial dates determine reporting facts; immutable confirmation order determines the approved stage-local/origin selections.                                                    |
| Explicit attribution correction and conceptual correction order                   | Sections 4.1, 7.2–7.3, 7.6; AC37, AC40, AC44–AC48                | Preserved: original facts → affected Allocations → forgiveness → credits. Direct reassignment after Allocations remains prohibited.                                                          |
| Participant archiving                                                             | Sections 4.1, 7.1; AC39                                          | Preserved. Historical attribution and balances remain stable; Participants are not deleted.                                                                                                  |
| Traceable correction/cancellation for confirmed Advances and Settlements          | Section 7.1; AC43, AC48; R01                                     | Q22 accepted. Drafts may be discarded; confirmed feature records are never hard-deleted. Inconsistent cancellation is blocked. Ordinary unrelated Finance v1 deletion behavior is unchanged. |
| Deterministic selection among affected Allocations and credit origins             | Sections 4.6, 5.7, 7.6; AC44–AC47; R02                           | Q23 accepted. Newest affected release; reverse original order within one Settlement; oldest originating receipt first for credit use; immutable ordering and provenance preserved.           |
| Existing Finance v1 date semantics                                                | Sections 5.3, 7.4; AC41, AC47                                    | Preserved. Future-dated recorded effects apply immediately; no scheduling or silent Allocation rewrite.                                                                                      |
| Accepted behavioral verification boundaries                                       | Section 9.1                                                      | Owner confirmation retained; no new test or feature implementation authorized.                                                                                                               |
| No historical migration or implementation/issue action                            | Sections 2.2, 8, 11–12                                           | Preserved.                                                                                                                                                                                   |

All known product decisions, including R01 and R02, are resolved. This
consistency review identified no remaining contradiction within the accepted
boundary. The following distinctions are explicit to avoid contradictory
interpretations:

- Ordinary oldest-Advance-date-first repayment and newest-confirmed-first
  correction release apply to different operations.
- Preserving truthful money does not preserve an incorrectly entered number;
  cancelling a duplicate removes its false financial effect while retaining
  correction history and every actual cash event.
- Immutable confirmation/origin ordering is separate from corrected financial
  event dates and does not rerun confirmed historical Allocations.
- Feature-specific traceable cancellation does not introduce global append-only
  accounting, merchant refunds, or general borrowing management.

## 11. Out of Scope

- General borrowing management or arbitrary unbacked credit creation. Explicit
  credit creation backed by an actual unallocated receipt is included.
- Multiple payers, mixed funding sources, group-wide netting, household/shared
  Ledger ownership, and managing other participants' books.
- Cross-currency settlement, FX, valuation, reporting/base currency, or totals
  combining unlike currencies.
- Interest, installments, recurring arrangements, scheduled jobs, and a dedicated
  loan-management product.
- Configurable settlement algorithms, manual per-Obligation amount editing as a
  required repayment step, and automatic historical FIFO redistribution.
- Dedicated merchant refund or later real-world renegotiation workflows.
- Participant collaboration, invitations, debt acknowledgement, notifications,
  payment collection, payment-provider integration, and Finance RBAC design.
- Historical receivable migration, automated extraction from old notes, and
  an opening-debt onboarding workflow.
- Full accounting reconciliation, period closing, a reports center, a general
  accounting audit/reversal system, or global append-only Finance history.
- Full split-category editing, receipt attachments/OCR, bank import, tax
  functionality, and unrelated Finance features.
- Unexplained balance overrides, automatic rounding/forgiveness/surplus acceptance,
  automatic credit consumption, and hidden netting across Obligations or people.
- Database, API, generated contract, UI, test, or infrastructure implementation
  within this documentation task; issue creation, commit, push, and publication.

Manual Finance primitives remain available for other bookkeeping, but ordinary
manual entries do not automatically maintain this feature's debt composition.
They cannot bypass linked-record protection. The product must not imply that an
excluded merchant-refund scenario has a dedicated supported resolution here.

## 12. Owner Decisions and Handoff Readiness

### 12.1 Accepted owner decisions

D01–D07 record the previously settled owner policies. Q22 and Q23 subsequently
resolved R01 and R02, recorded in section 12.2. All these policies form the
accepted product boundary; none is an unapproved implementation default.

| ID  | Decision                                  | Accepted policy                                                                                                                                                                                                                                    |
| --- | ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D01 | Consumed or returned credits              | Credit must not disappear implicitly during Correction. Preserve explicit origin, application, return, release/restoration, and resulting financial consequences. Preserve actual cash and require complete explicit handling before confirmation. |
| D02 | Participant reassignment                  | Once Allocations exist, direct reassignment is prohibited. Use explicit Correction semantics with original/corrected attribution and affected records visible; changing the debtor does not silently move a truthful payment.                      |
| D03 | Date corrections invalidating Allocations | Require explicit correction handling of invalid eligibility and linked consequences. Never automatically redistribute confirmed Allocations or leave invalid effective state.                                                                      |
| D04 | Participant lifecycle                     | Participants are archived, not deleted. Stable identity and historical attribution survive; archive does not change debt, credit, or recorded money.                                                                                               |
| D05 | Conceptual correction order               | Correct original facts → update affected Allocations → adjust forgiveness → adjust released/restored/created credits. This describes one consistent Correction, not independent intermediate commits.                                              |
| D06 | Future-dated entries                      | Follow Finance v1 calendar-date semantics, including immediate Current Balance effects for recorded future-dated events. Future/backdated changes cannot silently rewrite confirmed Allocation history.                                            |
| D07 | Receipts without eligible debt            | Permit explicit participant credit creation backed by actual unallocated receipts, even with no eligible outstanding Obligation. No implicit Income, fictitious Obligation, or automatic reversal of forgiveness.                                  |

### 12.2 Resolved R01 and R02

The owner approved Option A for Q22 and the complete recommended ordering
package for Q23. These replace the earlier pending clarifications.

| ID  | Resolved decision                                   | Accepted policy                                                                                                                                                                                                                                                                                                                                                                                                                    |
| --- | --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R01 | Advance and Settlement deletion/cancellation        | Drafts may be discarded. Confirmed Advances and Settlements use traceable Correction/cancellation and are never hard-deleted. Preserve actual cash, correction history, and all linked Allocation, forgiveness, and credit consequences. Do not silently reallocate payments or erase consumed/returned credit. Preview the complete result and block confirmation if consistency cannot be established.                           |
| R02 | Within-stage Allocation and credit-origin selection | Release newest affected Allocations first by immutable confirmation order with stable tie-breakers. Within one Settlement reverse its original confirmed allocation order. Apply or return credits oldest originating receipt first. Released/restored credit retains provenance and original ordering, independent of editable financial dates. Preview all consequences and never redistribute unrelated historical Settlements. |

No known product decisions remain unresolved. The selected rules do not reopen
any previously agreed scope, financial meaning, date behavior, correction stage,
or exclusion. Section 7 provides the operational rules and examples; all
acceptance criteria now describe settled product behavior.

### 12.3 To-tickets and review handoff

The specification is approved as authoritative product input for a to-tickets
breakdown. Independent product/domain review passed with non-blocking notes;
all product decisions and AC01–AC48 are frozen. The next step should propose
complete, independently verifiable vertical slices and their blocking
relationships for owner review, preserving this boundary. A proposed breakdown
requires its own approval before ticket publication; this document does not
create tickets or approve any breakdown. The repository's separate
implementation approval/delivery rules remain applicable before implementation.
The owner-accepted behavioral verification boundaries remain in section 9.1.

API and data representation, frontend interaction detail, and any glossary/ADR
updates are separate technical work. They are not remaining product decisions
in this specification and must not silently change the agreed product behavior.
No issue, implementation ticket, code, or generated contract is created here.
