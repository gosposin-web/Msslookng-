# ZAVOD 1.0

Five isolated Telegram accounts share panel login and the existing encrypted checkpoint. Slot 1 preserves the original account. API selection uses validated X-Story-Account and request-local ContextVars. Each slot has its own worker, audience, jobs and cooldown. Imports reject duplicate Telegram identities.

New view jobs react with 👍 only when a fresh lookup confirms the story author is non-Premium. Premium and unknown authors get views only. Existing jobs remain view-only. Durable intent records prevent automatic duplicate reactions after uncertain replies.

Account settings support bio, profile channels, creation of owned public channels and channel username edits. Channel creation is idempotent; partial successes and uncertain results are reported explicitly.

The phone interface has four primary tabs and a five-slot selector. Writes lock account switching. Responses from earlier account selections cannot replace the current state.

Validation: 183 Python tests passed; Node suite covers startup, gateway isolation, account switching, profile/channel forms and hosting quota state. No real Telegram operations are performed by tests.

Render suspended the existing free service on 2026-11-23 with actor Monthly Quota Exceeded. A temporary BACKEND_PAUSED_UNTIL gateway setting stops futile cold-start requests and expires automatically. The free plan and runtime secrets are preserved. The private Site keeps its existing audience and encrypted R2 snapshots.
