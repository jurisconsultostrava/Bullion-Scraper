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

### Supported Vendors
- StoneX Bullion (stonexbullion.com)
- European Mint (europeanmint.com)
- APMEX (apmex.com)
- BullionByPost (bullionbypost.co.uk)
- Generic fallback (any bullion dealer)

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
