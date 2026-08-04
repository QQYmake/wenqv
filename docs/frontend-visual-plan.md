# Frontend Visual Plan

## Goal

Refine Blue Lake Agent into a calm, high-end thinking workspace without changing its
chat, session, Skill, streaming, persistence, or tool-execution behavior. The bundled
LXGW WenKai Lite font files and their import remain unchanged.

The visual direction is **Editorial Lakehouse**: warm mineral paper, deep lake-teal
ink, restrained brass light, generous editorial spacing, and a clear split between
the welcome statement and the available starting actions.

## Architecture Boundary

The frontend enters through `web/index.html`, mounts from `web/src/main.tsx`, and is
orchestrated by `web/src/App.tsx`. `App` owns runtime state and delegates rendering to
`Sidebar`, `Welcome`, `MessageList`, `Composer`, and `ExecutionTrace`. All frontend
presentation rules currently live in `web/src/theme/styles.css`.

The browser communicates only through `web/src/api/client.ts` and its REST/SSE
contract. The backend keeps its hexagonal boundary:

- `server/api` translates HTTP and SSE events.
- `server/agent` owns agent policy and orchestration.
- `server/storage` owns persistence adapters.
- `server/main.py` composes concrete dependencies.

This redesign may change visual tokens, presentation-only markup, responsive layout,
and accessibility styling. It must not change API code, agent behavior, message
hydration, session operations, streaming event handling, or storage.

## Baseline Findings

The existing lake scene, custom icons, sun motif, dark mode, and local Chinese font
already give the product a specific identity. The main weaknesses are hierarchy and
readability:

1. The welcome screen uses a centered heading and three equal cards, which weakens
   visual priority.
2. Long-form Markdown uses a line height of `1`, making Chinese text difficult to
   read.
3. Several small muted labels and the light-theme focus outline have insufficient
   contrast.
4. The scrolling workspace uses a large `backdrop-filter`, which increases repaint
   cost over the animated lake.
5. Responsive rules hide suggestion actions instead of preserving them in a mobile
   stack.
6. The message viewport reserves a fixed composer height even though the composer can
   grow, so long drafts or selected Skills can cover the latest message.
7. Motion uses a mixture of custom and generic easing, so interactions lack a single
   physical rhythm.

## Design System Decisions

- Preserve LXGW WenKai Lite and create hierarchy with scale, spacing, color, and
  composition instead of depending on unavailable font weights.
- Use one desaturated lake-teal action color and reserve brass for the sun and small
  status accents.
- Treat the workspace, composer, and floating popovers as double-bezel surfaces:
  a quiet outer shell plus a concentric inner core.
- Convert the welcome area to an editorial split with left-led typography and a
  staggered vertical action composition.
- Keep all three suggestions available on mobile in a single-column stack.
- Use custom spring-like cubic Bezier curves and animate only transforms and opacity.
- Keep blur on fixed overlay layers only; scrolling content uses precomposed surfaces.
- Preserve visible focus states, reduced-motion behavior, and at least 44 px primary
  touch targets on narrow screens.

## Implementation Checkpoints

1. Establish accessible color, surface, spacing, focus, and motion tokens; refine the
   workspace shell.
2. Redesign the welcome and composer surfaces while preserving callbacks, labels,
   and keyboard behavior.
3. Refine conversation typography, execution traces, sidebar states, and responsive
   layouts.

After each visual checkpoint, run the frontend tests and production build. The final
gate also runs the Python suite and confirms that `web/public/fonts`, `web/src/api`,
and `server` are unchanged from the baseline commit.

