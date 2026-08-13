# Visitor Lounge Shared Codex Pipeline Design
## Goal

Keep the Visitor Lounge server, prompts, database, limits, queue, logs, and lifecycle independent while reusing AionsHome's existing local Codex installation, authentication profile, App Server transport, and token-saving invocation options.

## Boundaries

- Do not install another Codex CLI and do not create another login or lounge-specific `CODEX_HOME`.
- Do not modify AionsHome source files, configuration, database, memories, or running processes.
- Read only the existing Codex runtime assets under AionsHome: the project-local Codex package, the existing chat authentication profile, and the invocation options produced by AionsHome's Codex command builder.
- Keep the lounge prompt, visitor identity, summaries, recent messages, quota, output cap, unsafe-event rejection, queue, database, ports, and start/stop scripts inside `AionsHome-Visitor-Lounge`.
- Never pass AionsHome conversation, memory, worldbook, capability blocks, or owner identity into a visitor request.

## Design

`visitor_lounge.codex_adapter` remains the request-scoped protocol and safety boundary. A small lounge-owned runtime bridge locates the parent AionsHome checkout, imports only the existing Codex invocation helpers, and obtains:

- the project-local `Connor-Codex/node_modules/@openai/codex/bin/codex.js` path;
- the existing AionsHome chat environment/authentication profile;
- the current token-saving command overrides and App Server command shape.

The bridge replaces only the two owner-facing prompt overrides (`model_instructions_file` and `developer_instructions`) with lounge-owned values. All remaining AionsHome optimization overrides are preserved. The lounge continues sending its already bounded visitor prompt as the turn input and continues rejecting any capability/tool event.

The bridge fails closed before spawning if the parent checkout, local Codex package, expected AionsHome helpers, or authentication profile is unavailable. It never falls back to a global `codex` command and never launches an interactive login.

## Configuration and scripts

Remove `VISITOR_LOUNGE_CODEX_HOME` from lounge configuration and documentation. Remove `scripts/init-codex.ps1`. Diagnostics must verify the shared AionsHome local package and auth profile without changing either one.

The lounge keeps `.runtime/codex-workdir` as its empty request workspace. Service lifecycle remains independent on ports 8001 and 8002.

## Focused verification

1. Unit-test that the spawn command begins with Node plus AionsHome's local `codex.js`, contains the existing disable/trim overrides, contains lounge prompt overrides, and does not contain AionsHome owner-facing prompt overrides.
2. Unit-test that the environment uses the existing AionsHome chat authentication profile and that failures are closed before spawn.
3. Run only the affected lounge test modules and a compile check.
4. After local tests pass, start the lounge and send one short real message; do not run the full historical acceptance suite.
