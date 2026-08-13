# Denominate each Finance Account in one currency

Every Money value pairs an exact amount with a currency and never uses binary
floating-point semantics. Each Finance Account has exactly one currency, and
its Opening Balance and Account Movements use that currency. A Finance Ledger
may contain Finance Accounts in different currencies from the beginning.

## Considered alternatives

One implicit Finance Ledger currency would simplify Finance v1 but make
multi-currency account history costly to introduce later. Allowing each Account
Movement to choose a currency independently would leave a Finance Account's
balance without one stable denomination.

## Consequences

Finance v1 supports same-currency Internal Transfers but need not expose
cross-currency transfer workflows. The domain model must not require both sides
of every Internal Transfer to have equal nominal amounts, because a future
cross-currency transfer may record different exact amounts in the source and
destination currencies. Currency-aware precision is required, while integer
minor units versus fixed-decimal persistence remains an implementation choice.

Account Nature and currency define how existing Opening Balances and Account
Movements are interpreted, so an ordinary account edit must not change either
property underneath financial history. Whether an unused account can be
corrected before it has a financial position or transaction history remains a
future product rule.
