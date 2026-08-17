---
name: Cloudflare scraping via TLS impersonation
description: How to fetch bullion vendor sites behind Cloudflare bot protection; which profiles work and which sites are hard-blocked
---

# Cloudflare scraping via TLS impersonation

Cloudflare fingerprints the TLS handshake, so plain `requests`/`curl` with browser headers still gets 403 ("Just a moment..." challenge page).

**Rule:** Use `curl_cffi` with `impersonate=` profiles. As of July 2026, `firefox135` and `safari18_0` pass Cloudflare on bullionbypost.co.uk and apmex.com, while all chrome profiles (chrome131, chrome136, etc.) are challenged.

**Why:** Cloudflare's bot score differs per TLS fingerprint; chrome fingerprints are heavily abused so they get challenged more.

**How to apply:** Try a fallback chain of profiles (firefox → safari → chrome). Detect challenge pages by markers `"just a moment"` / `"challenges.cloudflare.com"` in the body and report a clear "requires JavaScript execution" error instead of a generic failure.

**Hard limits:**
- europeanmint.com runs an interactive Cloudflare JS challenge — no HTTP client passes it (no profile, no session/cookie warm-up). Would need a real headless browser.
- apmex.com returns 200 but renders its product grid client-side; server-rendered HTML has only a few promoted `/product/` links and no prices. Don't fall back to generic link scraping there — it only picks up nav labels.
