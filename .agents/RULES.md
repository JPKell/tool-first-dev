# Project Rules & Named Patterns

This file defines architectural conventions, named patterns, module boundaries, and documentation rules that mechanical linters cannot check. Mechanically checkable formatting and lint rules live in `.agents/ruff.toml`.

> Customize the roles, patterns, and Section 4 boundaries below to match your project's architecture (and keep `.agents/config.json` in sync).

---

## 1. Role Vocabulary

Every source module must declare **one** `Role` tag from `.agents/config.json`:

| Role | Responsibility | Boundary |
|---|---|---|
| `router` | HTTP/RPC/CLI endpoints, request parsing, auth guards, response formatting. | Thin layer only; delegate business logic to `service` and persistence to `repository`. |
| `service` | Core domain workflows, orchestration, external API clients, state transitions. | No raw transport/HTTP request objects; raises typed domain errors. |
| `repository` | Database queries, file persistence, transactions, schema/migrations. | Owns storage statements; returns typed models/dicts/dataclasses, no UI or HTTP concerns. |
| `schema` | Pydantic/dataclass DTOs, validation models, shared domain types. | Pure data definitions and validators; zero I/O or side effects. |
| `page` | Top-level UI views, screens, or page-level frontend controllers. | Coordinates components and API calls; keeps layout and state flow clear. |
| `component` | Reusable UI widgets, renderers, or client helpers. | Self-contained; communicates via callbacks/events rather than global state mutation. |
| `util` | Pure, stateless helper functions (parsing, formatting, crypto, math). | No database, network, or framework dependencies. |
| `test` | Automated tests verifying module contracts and edge cases. | Must remain deterministic and isolated (in-memory/temp DB, mocked network). |

---

## 2. Named Patterns

Reference these exact pattern names in module headers (`Patterns: ...`) so `get_conventions` and `find_similar` can retrieve real examples.

### `thin-router`
Entrypoints/routers validate input with schemas, enforce authentication/authorization, invoke a `service` or `repository`, and map domain exceptions to transport status codes. No business loops or raw persistence inside route handlers.

### `service-layer`
Core workflows live in focused service modules/classes with explicit dependencies injected at construction time so they are unit-testable without live servers or network I/O.

### `repository-pattern`
All persistence goes through focused repository modules using parameterized queries and explicit transactions. Callers never interpolate raw SQL or bypass repository boundaries.

### `structured-errors`
Raise explicit domain exception types carrying a machine-readable `code`, safe user-facing `message`, and optional `status_code`. Never leak secrets, tokens, or raw internal stack traces to callers or logs.

### `contract-first-test`
Tests are written before or alongside implementation, linked via the module's `Tests:` header field, and assert public behavior and invariants rather than private implementation details.

---

## 3. Documentation Contract

### Module Header (Required)
Every source file inside `header_required_dirs` (configured in `.agents/config.json`) must begin with a structured header. The fields only a human/agent can supply are required (`required_header_fields`):

- **Purpose** — One clear sentence describing what the module does.
- **Role** — Exactly one tag from the Role Vocabulary above.
- **Patterns** — Comma-separated list of pattern names from Section 2 (or `none` for trivial utilities).
- **Tests** — Relative path(s) to covering test file(s) (or `none` only for pure entrypoints/templates). Each path must exist.
- **Invariants** — Key assumptions or constraints that must remain true (or `none`).

Fields the code already knows are derived from the AST when omitted:

- **Exposes** — Public top-level classes/functions. If written, every name must be defined in the module (free-form entries such as routes, e.g. `GET /users`, are not checked).
- **Depends on** — Imported modules.

`lint_and_headers` reports **header drift** (an `Exposes` name that no longer exists, or a missing `Tests:` path) so headers cannot silently go stale. `scaffold_module` writes a valid header for you.

### Function & Class Docstrings
Public functions and classes should concisely document:
- Purpose (first line)
- Parameters (types + constraints)
- Return value
- Side effects (DB writes, network I/O, background tasks)

---

## 4. Project-Specific Boundaries & Security Rules

- Never commit `.env` files, API keys, credentials, or local `.agents/state/` artifacts.
- Never hard-code secrets, tokens, or environment-specific URLs in source code.
- Add your project's domain invariants and security boundaries here.
