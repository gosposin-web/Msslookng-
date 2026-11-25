# ZAVOD 1.0 — upgrade in progress

Requested scope: simpler phone interface; up to five Telegram accounts; Premium author = view only, non-Premium author = view plus a thumbs-up reaction; rename to ZAVOD 1.0; edit account bio; create and attach owned personal channels.

## Current draft

The accounts module provides five isolated table namespaces within the existing SQLite checkpoint file. The original account remains in slot 1. Panel login sessions remain shared. Each slot has its own Telegram gateway, worker, queue, runtime cooldown and audience. SQL parameters and quoted text literals are preserved. Root schema initialization also upgrades restored older checkpoints.

This branch is an implementation draft. It is not deployed. The execution environment became unavailable before validation; the tests added here have NOT been run.

## Remaining implementation

- Bind request account selection to ContextVars; forward the selected slot through the private Worker proxy.
- Start and stop all five account workers together, share snapshot dirty tracking, and serialize tdata imports. Reject the same Telegram identity in multiple slots.
- Persist Premium metadata and refresh it before sending a story reaction. An unknown Premium status must not trigger a reaction.
- Add durable reaction intent/result records. A lost RPC response must never cause an automatic duplicate reaction. Keep old view jobs as view-only; apply the new policy to newly created jobs.
- Add account bio read/write and channel creation/personal-channel assignment. Record channel creation intents and partial successes to prevent duplicate channels after a retry.
- Add an explicit account selector and simplify the existing interface; rename visible text, manifest and icon to ZAVOD 1.0.
- Run the legacy suite and the new account isolation, snapshot restore, reaction and channel tests.
- Publish the tested backend first, then the private Site, preserving audience, secrets, free hosting and the existing automatic startup flow.

## Validation required before release

Legacy snapshot migration; five-account isolation and concurrent context switching; per-account cooldown and revoked-session handling; no reaction for Premium/unknown targets; no duplicate reaction after interruption; channel partial failures and retry idempotency; mobile account switching without applying a stale result to another account.
