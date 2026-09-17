# 20260917-SDK-version-production-routing — Enable `version:"production"`, fix `"draft"` routing

- **Status:** Implemented (lands in both SDKs at `0.5.1`).
- **Date:** 2026-09-17
- **Author:** kii (with Claude)
- **Scope:** ships to **BOTH** SDKs together — `noukai-python-sdk` (`noukai-sdk`) **and** `noukai-typescript-sdk` (`@noukai/sdk`), same version, per the dual-land / parity contract (see `20260916-SDK-otel-and-replay-rename`).
- **Design-doc convention:** referenced from code/CHANGELOG as `20260917-SDK-version-production-routing`.

> **Repo note:** the parent `noukai-sdk/` is not itself a git repo — the two SDKs are. This doc lives at the parent and is mirrored into each repo's `docs/design-logs/2026/09Sep/` so it is version-controlled with the code.

---

## TL;DR

The SDK's version model was built against a **stale assumption** — that running the production version needed "server-side body-field routing" that "wasn't deployed." That routing was never built and never will be: the server routes versions **entirely by URL path**, and has done so, stably, since 2026-03 (`v0` alias) / 2026-07 (production fallback).

The actual, deployed server contract for `/execute` and `/jobs`:

| URL the SDK builds | Server resolves to |
|---|---|
| base path — `/{org}/{project}/{slug}/execute` (no version segment) | **production** (`flow.production_version_id`); falls back to draft/live when the flow has no published production version |
| `/{org}/{project}/{slug}/v0/execute` | **draft** (live working copy) — reserved alias |
| `/{org}/{project}/{slug}/vN/execute` (N≥1) | **published version N** |

`/step` (step-through SSE) is identical **except** it rejects `/v0` with `400 INVALID_VERSION` — draft is not supported for step-through.

Verified in the executor:
- `services/executor/router-ai-slugs/src/router_ai_slugs/api/seqflow_routes.py:356-441` (execute), `:1113-1193` (jobs), `:1406-1500` (step).
- `packages/seqflow-graph-service/src/seqflow_graph_service/service.py:70-74` (resolution priority: `use_draft` → `version_number` → `production_version_id` → draft/live fallback).
- `services/.../models/seqflow.py:49` — `SeqflowExecuteRequest` has **no** `version` field, confirming there is no body-field routing.

### The latent bug this exposes

The SDK maps `version:"draft"` (its documented default) to the **base path** — which the server resolves to **production**. So `flow.execute()` with no version has been silently running the published production version (and only behaving "like draft" for flows that were never published, where the server falls back to draft/live). There was also no way to reach the *actual* draft, and `"production"` threw a "not yet supported" error.

---

## Decision

Give the base path its correct name and make the three version values map to what the server actually does. **Public `VersionSpec` is unchanged (`"draft" | "production" | number`); the wire mapping and the default change.**

| `VersionSpec` | Wire segment | Server runs |
|---|---|---|
| `"production"` | base (no segment) | production |
| `"draft"` | `/v0` | draft (live working copy) |
| `N` (int ≥ 1) | `/vN` | version N |

- **Default when `version` is omitted → `"production"`** (execute / executeAsync / steps / events). This is **behavior-preserving**: every existing call already sent the base path on the wire. `"production"` just names what the base path always was; `"draft"` becomes explicit opt-in via `/v0`.
- **Streaming guard:** `steps()` / `events()` reject `"draft"` (and the equivalent integer `0`) client-side with a crisp error, mirroring the server's `/v0` step-through rejection, instead of a confusing 400 round-trip.
- **Removed dead field:** `ExecuteRequest.version` (TS `types/requests.ts`) was a never-serialized vestige of the abandoned body-routing plan; deleted.
- **Relay adapters** (`mount_flow_relay` / `createRelayFlow` / express / nextjs) accept `"production"`, default to `"production"`, and use the same mapping. (Relay forwards to `/execute`, which supports `/v0`, so relay `"draft"` works.)
- **Negative / non-integer** version ints are rejected client-side with a clear error (previously unvalidated).

### Why not keep default `"draft"`?

Considered and rejected. Making the default the real draft (`/v0`) would (a) change on-wire behavior for every published flow (default would flip from production to draft) and (b) break `steps()`/`events()` by default, since the server rejects `/v0` for step-through. Default `"production"` preserves today's behavior and is coherent across all four entry points.

---

## Implementation (mirrored, file-for-file)

| Concern | TypeScript | Python |
|---|---|---|
| Wire segment type | `paths.ts` `VersionSegment = "production" \| number`; `flowBase` base = production | `_paths.py` `VersionSegment`; `flow_base` base = production |
| Public → wire coercion | `flow.ts` `_pathVersion`: production→`"production"`, draft→`0`, N→N (reject bad ints) | `_flow.py` `_path_version` (sync + async classes) |
| Default | `flow.ts` execute/executeAsync `?? "production"`; `step-iterator.ts` `?? "production"` | `_flow.py` sync+async `version="production"`; `_step_iterator.py` |
| Remove throw | delete `version === "production"` errors | delete `NotImplementedError` branch |
| Streaming draft guard | `flow.ts` `steps()`/`events()` reject seg `0` | `_flow.py` sync+async `steps()`/`events()` |
| Dead body field | remove `ExecuteRequest.version` | (Python request builder never had it) |
| Relay | `adapters/relay.ts` `normalizeRelayVersion` + default; express/nextjs option types | `adapters/relay.py` `_normalize_version` + default |
| Version | `package.json` / `version.ts` → `0.5.1` | `pyproject.toml` / `_version.py` → `0.5.1` |
| CHANGELOG | `## [0.5.1]` **Changed / Fixed** + migration note | mirror |

---

## Test plan

Per repo, all existing suites stay green, plus:

- **Path mapping:** `"production"`→base, `"draft"`→`/v0`, `N`→`/vN` for execute, executeAsync (jobs), and step URLs (assert the exact URL hit by the fetch/transport mock).
- **Default:** omitting `version` hits the base path (production) — asserts behavior preservation.
- **`"production"` no longer throws:** the previous version-guard tests are inverted — it now executes and hits the base path.
- **Streaming draft guard:** `steps({version:"draft"})` / `events({version:"draft"})` (and `version:0`) throw a clear client-side error and never open a stream.
- **Bad int:** `version:-1` / non-integer throws.
- **Relay:** `"production"` accepted (base path), `"draft"`→`/v0`, unknown string rejected.
- **Parity:** `python scripts/check_parity.py` green (both `0.5.1`).

---

## References

- Server routes (verified): `seqflow_routes.py` (execute `:308`, jobs `:1064`, step `:1375`); resolution `seqflow-graph-service/service.py:58`.
- Dual-land / parity contract: `20260916-SDK-otel-and-replay-rename`.
- `v0`-draft alias origin: executor commit `6c962991` (2026-03-25); production fallback: `cdd851f3` (2026-07-25).
