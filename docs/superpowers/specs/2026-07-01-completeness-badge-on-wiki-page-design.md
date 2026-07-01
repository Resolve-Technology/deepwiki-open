# Completeness Badge on the Wiki Page — Design

**Date:** 2026-07-01
**Status:** Approved (design), pending implementation plan

## Problem

The TSD/BRD completeness summary is only visible in the Jobs panel, which shows
jobs finished within the last hour. After that window (or a page refresh once
the job ages out) there is no in-app way back to a wiki's completeness report —
the row just shows "done" with no summary. The report file itself is persistent
(served by `/api/wiki_completeness`), but nothing durable in the UI links to it.

## Goal

Show the completeness summary (and the "view full report" link) directly on the
wiki page for the currently-displayed version, so it is always reachable while
viewing that wiki — independent of any recent job.

## Non-goals (YAGNI)

- No new backend endpoint — the existing `GET /api/wiki_completeness` serves it.
- No restyle of the summary — reuse the Jobs-panel `CompletenessSummary`
  component verbatim (TSD line, BRD line, Impact-rows line, report link).
- No new report rendering — the "view full report" link keeps opening the raw
  Markdown (`format=md`), same as the panel.
- No change to the Jobs panel.

## Architecture

### Reused, unchanged
- `CompletenessSummary` and `completenessQuery` (`src/components/CompletenessSummary.tsx`)
  already render the TSD/BRD summary + link and build the query string from a
  `{ repo: {owner,repo,type}, language, provider, model }` shape.
- `GET /api/wiki_completeness?...&format=json|md` (via the Next proxy) already
  serves the report / null when absent.

### New component: `src/components/WikiCompletenessBadge.tsx`

A thin fetch-and-render wrapper (mirrors how `JobsPanel` fetches per-job):

```
WikiCompletenessBadge({ repoInfo, language, provider, model })
```

- `repoInfo` is the page's `effectiveRepoInfo` (`{owner, repo, type}` — the
  `RepoInfo` type already in use). `language`, `provider`, `model` are the
  currently-displayed version's values (`language`, `selectedProviderState`,
  `selectedModelState` on the wiki page — both provider and model are set from
  the loaded cache at page.tsx:318-323, so they identify the exact `provider~model`
  report file).
- On mount and whenever `repoInfo`/`language`/`provider`/`model` change, if
  BOTH `provider` and `model` are non-empty, fetch
  `/api/wiki_completeness?${completenessQuery({repo: repoInfo, language, provider, model})}&format=json`.
  Store the parsed report (or `null`) in local state.
- Render `<CompletenessSummary report={report} mdHref={<same query>&format=md} />`.
- Render nothing (`null`) when: provider or model is empty, the fetch throws or
  returns non-OK, or the report is `null` (older wikis without a report).
  `CompletenessSummary` itself already returns nothing for a `null` report.
- The fetch is best-effort: any error resolves to `null` (no report shown); it
  never throws into the page.

### Integration: `src/app/[owner]/[repo]/page.tsx`

Render `<WikiCompletenessBadge repoInfo={effectiveRepoInfo} language={language}
provider={selectedProviderState} model={selectedModelState} />` in the wiki
header/meta area (near where the generated-at / provider metadata is shown at the
top of the wiki view), so it appears once per displayed wiki, above the content.

## Data flow

Wiki page loads the cache → `selectedProviderState`/`selectedModelState`/
`language`/`effectiveRepoInfo` reflect the displayed version → `WikiCompletenessBadge`
fetches that version's report via the existing proxy → renders `CompletenessSummary`
in the header → "view full report ↗" opens the Markdown report.

## Error handling

- Missing provider/model (e.g. before the cache resolves) → badge renders
  nothing; it re-fetches once they are set (they are in the effect deps).
- Fetch/network error or 404 → treated as "no report", badge renders nothing.
- No report on disk (older wiki) → endpoint returns 200 null → badge renders
  nothing. The wiki page is never blocked or errored by the badge.

## Testing

- `CompletenessSummary` and `completenessQuery` already have vitest coverage
  (rendering of the by_document + fallback branches; query params). No new logic
  lives in them.
- `WikiCompletenessBadge` is a fetch-driven effect wrapper; like `JobsPanel`'s
  own fetch effect it is not unit-testable in the repo's node/no-jsdom vitest
  env. It is verified by: (1) `tsc --noEmit` type-check (props wired correctly,
  reuses `CompletenessSummary`/`completenessQuery`); (2) an e2e check — open a
  wiki that has a completeness report (e.g. bv401) and confirm the badge renders
  the TSD/BRD summary + working "view full report" link, and that a wiki without
  a report shows no badge.
- If a pure seam is wanted for a unit test, the `mdHref`/query construction is
  already `completenessQuery` (tested); no additional pure helper is introduced.

## Scope

One new ~30-line component plus a one-line render in `page.tsx`. No backend, no
new endpoint, no dependency changes, no Jobs-panel changes.
