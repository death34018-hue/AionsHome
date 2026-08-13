# ChatGPT Web OAuth Bridge Design

## Goal

Allow ChatGPT web plugins to connect to `https://visitor.aionshome.com/mcp`
without exposing a static Visitor Key to ChatGPT. A friend enters the owner-issued
Visitor Key once in a lounge-hosted authorization page. Every resulting OAuth
request resolves to the same existing visitor ID, messages, rolling memory, quota,
lock state, and fixed display name as the web lounge and static-key MCP clients.

## Boundaries

- Keep the visitor service on `127.0.0.1:8001`, admin on `127.0.0.1:8002`, and
  the existing Cloudflare tunnel and `/mcp` endpoint.
- Do not add an account, password, email login, process, port, database, or global
  installation.
- Preserve direct `Authorization: Bearer <Visitor Key>` MCP authentication.
- OAuth authorizes only scope `visitor:lounge` for resource
  `https://visitor.aionshome.com/mcp`.
- The lounge continues to accept text only; OAuth cannot access local files,
  management endpoints, or other AionsHome functions.

## Protocol

The existing official MCP Python SDK hosts OAuth 2.1 discovery, dynamic client
registration, authorization, token, revocation, protected-resource metadata, and
PKCE validation. The built-in provider stores registered ChatGPT client metadata
encrypted with the lounge master key.

On authorization, the provider persists a short-lived pending request and redirects
the browser to `/oauth/consent`. The page explains the binding and asks for the
Visitor Key. A valid active Key creates a single-use authorization code tied to that
exact `visitor_key` row. The browser is redirected only to the URI previously
validated by the SDK against the registered client.

The token endpoint exchanges the code for random opaque access and refresh tokens.
Only keyed hashes are stored. Access tokens live for one hour; rotating refresh
tokens live for 30 days. Authorization requests live for 10 minutes and codes for
five minutes. Token lookup joins the original Key and visitor so a Key rotation,
revocation, visitor suspension/deletion, expiry, wrong client, wrong scope, or wrong
resource invalidates access.

## Persistence

Add four visitor-owned SQLite tables:

- `oauth_clients`: encrypted dynamic-client metadata.
- `oauth_pending_authorizations`: hashed browser request handles and PKCE request data.
- `oauth_authorization_codes`: hashed single-use codes bound to visitor and Key.
- `oauth_tokens`: hashed access/refresh tokens grouped into revocable families.

Visitor deletion cascades through codes and tokens. Client deletion cascades through
its OAuth records. Expired rows may remain harmlessly until later maintenance; all
reads enforce expiry.

## Compatibility and errors

All six tools advertise OAuth scope metadata. Unauthenticated MCP responses retain
the SDK's RFC 9728 challenge. The consent page never identifies whether an invalid
Key once existed and applies the existing login attempt limiter. Cancellation and
expired requests return a quiet, non-secret-bearing error page. No credential,
authorization code, access token, refresh token, or token hash appears in logs,
audits, model prompts, or UI error text.

## Verification

Use focused tests only: schema creation/cascade, DCR persistence, authorization-code
exchange, PKCE-compatible payloads, refresh rotation, static-Key compatibility,
resource/scope checks, Key-rotation invalidation, discovery endpoints, and the
consent form. Finish with compile/import and local/public discovery checks. Do not
call `talk_to_host` or run the full heavy suite.
