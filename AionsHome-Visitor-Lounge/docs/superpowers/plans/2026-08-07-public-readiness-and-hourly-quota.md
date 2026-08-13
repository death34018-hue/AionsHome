# Public Readiness and Hourly Quota Plan

**Goal:** Finish the lightweight public rollout and let the local administrator
change the per-visitor hourly chat allowance without restarting the Lounge.

## Scope

1. Add an `hourly_quota_limit` reception setting with a default of 10 and an
   allowed range of 1–500.
2. Change new quota windows from 24 hours to one hour. Saving the setting updates
   active windows immediately; raising the limit grants capacity immediately,
   while lowering it never removes already-recorded usage.
3. Mark the visitor session cookie `Secure` when the original request is HTTPS,
   including HTTPS forwarded by Cloudflare, while preserving local HTTP login.
4. Reject public request bodies larger than 16 KiB before application handling.
5. Replace generic/non-JSON network failures with a friendly reconnect-and-resend
   message and preserve the visitor's unsent text.
6. Update operating documentation to describe the live Cloudflare route and the
   configurable hourly quota.

## Lightweight verification

- Compile Python sources and parse the schema in memory.
- Check JavaScript syntax and Git whitespace.
- Restart only Visitor Lounge and confirm both local health endpoints.
- Confirm the public login page responds through Cloudflare.
- Inspect the migrated setting and active quota window without running the full
  test suite or consuming a model generation.
