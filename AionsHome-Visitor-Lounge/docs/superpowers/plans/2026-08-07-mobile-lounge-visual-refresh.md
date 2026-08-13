# Mobile Lounge Visual Refresh Plan

**Goal:** Rebuild the visitor-facing Lounge as a light, minimal, phone-first
experience inspired by the supplied reference while preserving every existing
login, claim, quota, safety, queue, and chat behavior.

## Design

- Use a warm ivory canvas, restrained sand-gold accents, mist-blue visitor
  bubbles, soft borders, and very light shadows.
- Render full-screen on phones and as a centered 430–460 px mobile canvas on
  wider screens.
- Show the configured current host as selected and online. Show one configured
  standby host as unavailable, without a fake switching action.
- Keep the public Key gate as the first interactive step.
- Present visitor identity, current hourly quota, and session state in a compact
  three-column card.
- Use the provided host portrait and the existing Aion image only after copying
  them into the Lounge-owned static directory.
- Keep character names configuration-driven; templates must not hardcode them.

## Files

- Add `static/avatars/host.jpg` and `static/avatars/standby.png`.
- Add `static/visitor.css` and switch only visitor templates to it.
- Update `config/visitor-lounge.toml`, `settings.py`, `visitor_app.py`, and
  `visitor_service.py` for the standby display name.
- Update the three visitor templates and `static/visitor.js`.

## Lightweight verification

- Compile Python and check JavaScript syntax.
- Restart Visitor Lounge and confirm both health endpoints.
- Inspect login, claim, and chat layouts at a phone-sized viewport without
  consuming a model response.
- Confirm the public page still begins at the invitation Key gate.
