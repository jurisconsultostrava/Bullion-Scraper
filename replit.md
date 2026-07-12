# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.
Also includes a standalone Python/Flask bullion scraper app.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)
- **Python**: 3.11 (Flask bullion scraper)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

## Bullion Scraper (Python/Flask)

Located at `artifacts/bullion-scraper/`

### Files
- `app.py` — Main Flask app with all routes and vendor adapters
- `requirements.txt` — Python dependencies
- `Procfile` — gunicorn deployment command
- `templates/index.html` — Frontend UI

### Routes
- `GET /` — HTML frontend
- `GET /health` — Health check JSON
- `GET /fetch?url=...` — Server-side HTML fetch (proxy)
- `GET /scrape?url=...` — Full scrape + parse pipeline
- `POST /parse?vendor=...` — Parse raw HTML body

### Product Schema
Normalized items include `product_number`: BullionByPost reads it from
`data-price-product-id` on listing cards; StoneX has no number on listing
pages, so `/scrape` enriches concurrently from each product detail page
(JSON-LD `"sku"`, ThreadPoolExecutor, in-memory cache keyed by product URL).
Enrichment is host-restricted to stonexbullion.com (SSRF guard); vendor
detection uses exact/subdomain host matching, never substrings.

### Supported Vendors
- StoneX Bullion (stonexbullion.com) — full support
- BullionByPost (bullionbypost.co.uk) — full support (hub + listing pages)
- APMEX (apmex.com) — partial: product grid is client-side JS; only the few server-rendered `/product/` links are returned, usually without prices
- European Mint (europeanmint.com) — blocked: Cloudflare JS challenge cannot be passed server-side; returns 502 with a clear error message
- Generic fallback (any bullion dealer)

### Fetching
`fetch_html()` uses `curl_cffi` with TLS browser impersonation (profiles tried in
order: firefox135, safari18_0, chrome131). Cloudflare fingerprints the TLS
handshake, so plain `requests` gets 403 on several vendors; firefox/safari
profiles currently pass where chrome is challenged. Fetch failures raise
`FetchError` (with upstream status) and surface as 502 JSON errors.

### Running Locally
```
cd artifacts/bullion-scraper
PORT=5000 python app.py
```

### Deployment
```
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60
```

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.
