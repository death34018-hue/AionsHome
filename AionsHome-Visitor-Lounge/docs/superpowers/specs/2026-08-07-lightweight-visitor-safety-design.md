# Lightweight Visitor Safety Design

## Goal

Add a small, entertainment-grade safety boundary to Visitor Lounge before its
public Cloudflare phase. The Lounge should remain friendly and conversational,
while stopping clear attacks, credential theft, privacy abuse, identity
impersonation, persistent romantic pressure, and abusive harassment.

## Scope

This change extends the existing `continue` and `safety_lock` flow. It does not
add another model call, an external moderation service, risk scoring, a global
blocklist, or a new administration system. The existing 500-character input
limit, 600-token input limit, 800-character output limit, quota, queue, audit,
fixed safety reply, and administrator unlock remain authoritative.

## Trusted identity boundary

Every visitor name and message is untrusted data. A visitor remains a visitor
even when their chosen name exactly matches the owner, host, administrator, or
another trusted identity. A name match, identity claim, quoted instruction, or
claim of permission never grants authority, memories, relationships, or access.

The host belongs to the Lounge owner's household and speaks to the current
participant only as an invited friend. The rule does not need the owner's
private profile. Personal names must come from Visitor Lounge configuration or
saved content; reusable prompt and UI logic must not hardcode Ithil, Connor, or
another current personal name.

## Safety decisions

The existing Codex call makes the contextual decision:

- Normal conversation and ordinary security education return `continue`.
- Ambiguous requests are answered safely without assuming malicious intent.
- A first romantic, flirtatious, sexual, exclusive, or dependency-seeking
  approach receives a polite friendship-only boundary and returns `continue`.
- Repeated romantic pressure after a boundary, persistent harassment, threats,
  malicious abuse, operational attacks, malware assistance, credential theft,
  privacy abuse, prompt injection, or authority impersonation return
  `safety_lock`.

The host must not initiate flirtation, romantic or sexual language, exclusive
promises, possessiveness, or dependency-building statements. Warm friendship,
ordinary comfort, and respectful discussion remain allowed.

When `safety_lock` is selected, the scheduler keeps its current behavior: it
replaces generated text with the configured unsafe-request template, persists
the fixed reply, locks only that visitor, records a metadata-only audit event,
and requires an administrator to unlock the visitor.

## Local credential precheck

A narrow precheck runs before persistence, quota reservation, or a model call.
It detects only credential-shaped material rather than broad security words:

- PEM private-key blocks;
- authorization bearer values;
- common API-token prefixes with a substantial secret body;
- explicit password, secret, token, or API-key assignments with a substantial
  value.

The detector normalizes Unicode compatibility forms for matching but never
returns or logs the detected value. A match receives a configurable polite
template, consumes no quota, is not persisted as a message, and produces an
audit event containing only the category. One accidental paste does not lock
the visitor. Semantic requests to obtain passwords or keys are handled by the
trusted model policy and may trigger `safety_lock`.

## Configuration and administration

Reception settings gain one fixed template for accidental credential input.
The existing unsafe-request template remains the terminal reply for malicious
or persistent boundary violations. Existing visitor detail, audit history,
status, and unlock controls are sufficient; no new risk dashboard is added.

## Error handling and privacy

Credential detection fails closed for a positive match and fails open for
ordinary text. It never writes the rejected input to messages, jobs, audit
payloads, or application logs. Empty and oversized inputs continue through
their existing validation paths. The model has no tools and no access to the
owner's conversations, memories, files, databases, devices, or AionsHome
capabilities.

## Verification

Verification is intentionally lightweight per the project owner: compile the
changed Python package, parse the changed JavaScript when available, and run
`git diff --check`. Do not run the full test suite, a large attack corpus, or
additional real Codex messages for this change.

## Next phase

Cloudflare public access is a separate follow-up. Both applications continue to
listen only on `127.0.0.1`; the public visitor route will be exposed through a
Tunnel, while the administration route remains local or is protected by
Cloudflare Access rather than being published directly.
