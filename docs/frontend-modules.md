# Browser module boundaries

SagaSmith Web serves the browser client directly from FastAPI without a frontend build step. The
client therefore uses native ES modules and keeps every runtime asset inside the Python package.
The HTML shell, PWA manifest, service worker, styles, icon, and `app.js` remain available at their
existing root URLs. Imported JavaScript modules use the dedicated `/assets` static namespace so
they cannot be confused with JSON endpoints under `/api`.

## Current ownership

| Path | Responsibility |
|---|---|
| `web/api/client.js` | Same-origin JSON/FormData requests and consistent API errors |
| `web/auth/controller.js` | Session bootstrap, login, registration, logout, and auth visibility |
| `web/campaign/controller.js` | Campaign list, campaign creation, and invite acceptance |
| `web/components/dom.js` | Small DOM element and selector helpers |
| `web/components/toast.js` | Transient status messages |
| `web/components/pwa.js` | Service-worker registration |
| `web/forge/catalog.js` | Forge discovery, artifact details, discussions, installs, forks, and reports |
| `web/forge/studio.js` | Creator Studio artifacts, release drafts, Agent review, and submission |
| `web/forge/moderation.js` | Administrator release and report decisions |
| `web/forge/shared.js` | Forge type labels and release JSON parsing |
| `web/forge/controller.js` | Forge-internal composition and the public catalog/studio entrypoints |
| `web/identity/controller.js` | Hosted Identities, assignments, isolated memory, invitations, and room hosting |
| `web/module-studio/controller.js` | Module Studio project, run, source, install, and publish flow |
| `web/room/model.js` | Shared Room selectors for actor authority, action context, and combat projections |
| `web/room/view.js` | Room-only detail-list and numeric rendering helpers |
| `web/room/timeline.js` | Message presentation, suggestions, snapshot hydration, and ordered SSE handling |
| `web/room/characters.js` | Character drawer, private-card loading, pages, controls, and action context |
| `web/room/combat-grid.js` | Combat panel, initiative, grid interaction, and Canvas rendering |
| `web/room/controller.js` | Live Room lifecycle, membership/DM tools, panel orchestration, and Room composition |
| `web/state/store.js` | Shared browser-session state owned by the composition root |
| `web/app.js` | Cross-feature composition, navigation, and the remaining Pack/usage flows |

The first stage reduces `app.js` from 84,293 bytes to about 72 KB while moving a complete product
surface (Module Studio) and the shared infrastructure behind explicit imports. The service-worker
shell precaches the complete static import graph, preserving warm and offline PWA startup.

The second stage moves Forge catalog, Creator Studio, moderation, and hosted Identity/assignment
flows behind feature controllers. It reduces `app.js` again, from about 72 KB to about 56 KB. The
composition root injects Pack loading and Identity Soul-option loading into Forge, so controllers
do not import one another and the existing API, DOM, and authoritative MCP behavior stays intact.

The third stage moves the complete Live Room surface behind a Room-local composition boundary:
timeline/SSE, character drawer, and combat grid are separate modules, while membership, panel, and
Room lifecycle orchestration remain in `room/controller.js`. It reduces `app.js` from 56,355 bytes
to 4,496 bytes (about 92%). Campaign refresh, hosted Identity refresh, Module Studio navigation,
and usage refresh enter the Room only as injected callbacks. The SSE handlers retain their prior
registration and callback order, and browser requests still rely on server-derived principals and
the authoritative MCP-backed Room endpoints.

## Dependency direction

Feature controllers may import `api`, `components`, and `state`. Shared infrastructure must not
import a feature controller. `app.js` is the only composition root: it wires callbacks between
features when one feature needs another, rather than introducing feature-to-feature imports.

Within `web/room`, `controller.js` is a feature-local composition layer. `timeline.js`,
`characters.js`, and `combat-grid.js` may import only Room selectors/view helpers and shared
infrastructure; they do not import `controller.js` or another product feature. Character/combat
callbacks are wired by `controller.js`, which keeps the static ES-module graph acyclic.

Every new module must be added to the service-worker shell or loaded through an intentionally
documented runtime-cache path. The web-asset regression test walks the static import graph, checks
that FastAPI serves every module as JavaScript, verifies that each imported module is precached,
and rejects circular imports. CI syntax-checks every packaged browser JavaScript file rather than
only the entrypoint.

## Next reviewable extractions

Continue in product-sized increments rather than moving arbitrary line ranges:

1. extract the small Pack and usage flows into their own controllers;
2. leave `app.js` as navigation and controller composition only;
3. consider finer Room splits only when behavior or ownership grows beyond the current boundaries.

These later steps should keep the same APIs, DOM IDs, authoritative MCP boundary, and no-build
deployment model unless a separate architecture change is approved.
