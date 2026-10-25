# Chasing Grails (public copy)

Static mirror of the Chasing Grails dashboard: official TCG drops, drawings, big-box restocks, game-store finds, conventions and card shows.

- `index.html`: the page (no build step).
- `data/*.json`: the datasets. The twice-daily scan updates these files and `data/meta.json`; Vercel redeploys on each push.
