# Engineering walkthrough

## 1. Keep the core useful without a model

Tasks, calendars, habits and activity summaries are normal application features with deterministic domain services. An unavailable LLM should not make a household unable to read its plans or complete a task. The model adapters in `familyos/companion.py` add natural-language interaction around that core.

Tradeoff: this requires maintaining explicit tool schemas and context selection rather than allowing a model to operate directly on storage. The benefit is that the application's permission boundary remains inspectable and testable.

Read: `familyos/life.py`, `familyos/companion.py`, `tests/test_companion.py`.

## 2. Treat model actions as proposals

The assistant can propose task creation, task completion and calendar creation. A proposal is tied to a member and conversation. Confirmation re-enters application validation; it does not execute arbitrary model-provided SQL or shell text. Rejection, expiry and cancellation have explicit outcomes.

The useful review question is: **can an untrusted response cause a write before the member confirms it?** Inspect the proposal and execution paths together with the HTTP tests rather than judging the prompt alone.

Read: `familyos/companion_actions.py`, `familyos/agent_jobs.py`, `tests/test_http.py`, `tests/test_agent_jobs.py`.

## 3. Make household ownership part of persistence

Tasks and calendar entries carry household and owner identifiers, with explicit visibility. The current member is resolved from the server-side session. Mutation services check ownership before changing rows; the demo exercises both a shared and a private task using separate cookie jars.

SQLite was chosen for a single-host deployment with straightforward backup and inspection. Connections enable foreign keys and WAL mode; migrations make schema evolution visible. This is not a claim of multi-region durability or high-write-throughput scaling.

Read: `familyos/auth.py`, `familyos/member_context.py`, `familyos/db.py`, `migrations/`, `tests/test_member_context.py`.

## 4. Minimize activity data at the input boundary

The optional Windows agent processes screenshots locally. Server ingestion rejects raw screenshot fields; accepted records contain redacted observations and metadata. Summaries publish their observation windows and sample limits instead of treating screen time as proof of productivity.

The public demo does not activate screen capture. Agent source is included for review, while installation into a user's background environment is a separate action.

Read: `agents/windows_activity/collector.py`, `familyos/activity.py`, `familyos/analytics.py`, `tests/test_activity.py`.

## 5. Bound background work

Scheduled jobs need explicit lifecycle states, deduplication and cancellation. Website monitoring also needs URL restrictions, cooldowns and controlled notification destinations. Tests exercise these behaviors without sending real notifications or calling a paid model.

Read: `familyos/agent_schedule.py`, `familyos/monitoring.py`, `familyos/task_webhooks.py` and their test modules.

## 6. Separate the public artifact from production

This repository publishes a runnable API snapshot with synthetic fixtures and its own CI. It contains no historical household deployment records or credentials. The private UI package and fonts are outside this distribution; the only bundled HTML is a small API information page.

The public Compose file is deliberately self-contained: one application service, a local database volume and a loopback port. CI tests a freshly built container with outbound networking disabled. Public commits never deploy to the household server.

## Current limitations and next engineering decisions

- Username-based sign-in is suitable only for trusted local evaluation. Strong identity verification must precede any shared internet deployment.
- Long-lived worker coordination assumes one application host; horizontal scaling would require a shared job/lease strategy.
- Private-network service discovery is opt-in and requires trusted configuration. Example status links use placeholder addresses.
- Real speech, live models and external source behavior require separate integration testing. The offline demo makes no claims about those services.
- Home Assistant is an intended integration boundary, not a completed general-purpose device control implementation.
