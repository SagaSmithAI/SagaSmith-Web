# Browser module boundaries

SagaSmith Web serves the browser client directly from FastAPI without a frontend build step. The
client therefore uses native ES modules and keeps every runtime asset inside the Python package.
The HTML shell, PWA manifest, service worker, styles, icon, and `app.js` remain available at their
existing root URLs. Imported JavaScript modules use the dedicated `/assets` static namespace so
they cannot be confused with JSON endpoints under `/api`.

## First-stage ownership

| Path | Responsibility |
|---|---|
| `web/api/client.js` | Same-origin JSON/FormData requests and consistent API errors |
| `web/auth/controller.js` | Session bootstrap, login, registration, logout, and auth visibility |
| `web/campaign/controller.js` | Campaign list, campaign creation, and invite acceptance |
| `web/components/dom.js` | Small DOM element and selector helpers |
| `web/components/toast.js` | Transient status messages |
| `web/components/pwa.js` | Service-worker registration |
| `web/module-studio/controller.js` | Module Studio project, run, source, install, and publish flow |
| `web/state/store.js` | Shared browser-session state owned by the composition root |
| `web/app.js` | Composition plus the still-coupled room, Forge, Identity, Pack, and usage flows |

The first stage reduces `app.js` from 84,293 bytes to about 72 KB while moving a complete product
surface (Module Studio) and the shared infrastructure behind explicit imports. The service-worker
shell precaches the complete static import graph, preserving warm and offline PWA startup.

## Dependency direction

Feature controllers may import `api`, `components`, and `state`. Shared infrastructure must not
import a feature controller. `app.js` is the only composition root: it wires callbacks between
features when one feature needs another, rather than introducing feature-to-feature imports.

Every new module must be added to the service-worker shell or loaded through an intentionally
documented runtime-cache path. The web-asset regression test walks the static import graph, checks
that FastAPI serves every module as JavaScript, and verifies that each imported module is precached.

## Next reviewable extractions

Continue in product-sized increments rather than moving arbitrary line ranges:

1. extract Forge catalog, Creator Studio, and moderation into `web/forge`;
2. extract hosted Identity and campaign-host assignment into `web/identity`;
3. split room presentation/SSE from character and combat-grid controllers under `web/room`;
4. leave `app.js` as navigation and controller composition only.

These later steps should keep the same APIs, DOM IDs, authoritative MCP boundary, and no-build
deployment model unless a separate architecture change is approved.
