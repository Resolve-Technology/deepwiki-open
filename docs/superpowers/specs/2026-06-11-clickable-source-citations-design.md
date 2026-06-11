# Clickable Source Citations — Design

**Date:** 2026-06-11
**Status:** Approved (pending spec review)

## Problem

Generated wiki pages cite sources as markdown links with an **empty href**:

```
Sources: [CAL101.txt:51-54]()
```

The link *text* (`CAL101.txt:51-54`) carries the file and line range, but the `()` href is
intentionally blank (`api/wiki_prompts.py:313,491`). `src/components/Markdown.tsx` renders these
through a plain `<a href="">` handler, so clicking does nothing — the citation is a dead link.

Separately, the real blob links in each page's top-of-page `<details>` block are built with a
trailing `.git` in the URL (e.g. `…code2_sqlcbl_cal101.git/-/blob/main/CAL101.txt`), which 404s on
GitLab/GitHub.

## Goal

Make a `Sources: [file:lines]()` citation open the exact lines in the repository's web UI
(GitLab `/-/blob/`, GitHub `/blob/`, Bitbucket `/src/`). Do it at **render time** so every existing
cached wiki benefits immediately — no regeneration. Also fix the `.git` URL bug.

## Non-goals (YAGNI)

- In-app source code viewer / modal (that was option 2; not chosen).
- Threading `repoInfo` into the Ask, Workshop, and WikiReview Markdown call sites (the new prop is
  optional; those can adopt later).
- Regenerating existing wikis.

## Approved decisions

- **Branch:** parse the real branch from the full blob URLs already present in each page's top
  `<details>` block; fall back to `'main'`. No config, always matches what the generator used.
- **Local repos:** render the citation as plain, non-clickable styled text (no dead link), since
  there is no remote URL to point at.
- **`.git` bug:** fix in both the frontend URL builder and the Python `generate_file_url`. The
  Python fix only affects newly-generated wikis.
- **Test tooling:** add `vitest` as a dev dependency (no JS test runner exists today) and unit-test
  the pure util.

## Components

### 1. `src/utils/citationUrl.ts` (new — pure functions)

- `parseCitation(text: string): { filePath: string; startLine?: number; endLine?: number } | null`
  - Regex over the link *text*. Matches `filePath` (may contain `/`), optional `:start` and
    optional `-end`. Returns `null` when the text is not a citation (e.g. a normal link label).
  - Examples: `CAL101.txt:51-54` → `{filePath:'CAL101.txt',startLine:51,endLine:54}`;
    `copybook/CLNMSKM.txt:12` → single line; `README.md` → whole file (no lines).
- `buildBlobUrl(repoInfo: RepoInfo, filePath: string, branch: string): string | null`
  - Mirrors `api/wiki_prompts.py:generate_file_url`. github → `/blob/<branch>/`,
    gitlab → `/-/blob/<branch>/`, bitbucket → `/src/<branch>/`.
  - Strips a trailing `.git` from the repo URL; normalizes a trailing `/`.
  - Returns `null` for `type === 'local'` or when no usable `repoUrl` can be derived.
- `lineAnchor(repoType: string, start?: number, end?: number): string`
  - GitHub: `#L51-L54` / `#L51`. GitLab: `#L51-54` / `#L51`.
  - Bitbucket: **file-level only** — return `""` (no line anchor). Bitbucket's anchor format is
    unverified; a working file-level link is better than a wrong anchor. Revisit if a Bitbucket
    repo is ever used.
  - Empty string when no line numbers.
  - NOTE before shipping: spot-check the GitLab range form (`#L51-54`, end has no `L`) against a
    real GitLab blob page — it differs from GitHub's `#L51-L54`.
- `extractDefaultBranch(content: string): string`
  - Finds the first blob URL in the markdown (`/-/blob/<branch>/`, `/blob/<branch>/`, or
    `/src/<branch>/`) and returns `<branch>`; falls back to `'main'`.
- `buildCitationHref(repoInfo, branch, text): string | null`
  - Convenience composition: `parseCitation` → `buildBlobUrl` + `lineAnchor`. `null` when not a
    citation or no URL can be built.
  - NOTE: the renderer does NOT rely solely on this, because it must distinguish "not a citation"
    (render a normal link) from "is a citation but no buildable URL" (render plain text). The
    renderer calls `parseCitation` first, then `buildBlobUrl`, and branches on each result. See §2.

### 2. `src/components/Markdown.tsx`

- Add optional prop `repoInfo?: RepoInfo`. Existing `content` prop unchanged.
- `const defaultBranch = useMemo(() => extractDefaultBranch(content), [content])`.
- Rewrite the `a` renderer. The decision order matters — parse first, then branch on results.
  "Render default" everywhere below means render the **existing** anchor verbatim:
  `<a href={href} target="_blank" rel="noopener noreferrer" className="text-purple-600 …">{children}</a>`.
  1. If `href` is truthy (a real link, e.g. the `<details>` blob links) → **render default**, unchanged.
  2. Extract a plain string from `children` (handle string or array of text nodes). If not cleanly
     stringifiable → **render default**.
  3. `const cite = parseCitation(text)`. If `cite` is `null` (text isn't a citation) → **render default**.
  4. `const url = repoInfo ? buildBlobUrl(repoInfo, cite.filePath, defaultBranch) : null`.
     - If `url` is non-null → render `<a href={url + lineAnchor(repoInfo.type, cite.startLine, cite.endLine)}
       target="_blank" rel="noopener noreferrer">` with the existing link styling.
     - If `url` is `null` (local repo / no repoUrl) → render a plain `<span>` with muted,
       non-clickable styling showing the citation text.

### 3. `src/app/[owner]/[repo]/page.tsx` (line ~1162)

- Pass `repoInfo={effectiveRepoInfo}` to `<Markdown>`.

### 4. `api/wiki_prompts.py` `generate_file_url` (lines ~31-49)

- Strip a trailing `.git` from `repo_url` before composing the blob URL.

## Edge cases

- Citation paths with directories (`copybook/X.txt:1-9`) — regex allows `/`.
- Whole-file citation (no `:line`) — link to file, no anchor.
- Single line (`:51`) — single-line anchor.
- Non-citation empty-href links — left untouched (won't match the pattern).
- `repoUrl` with trailing slash or `.git` — normalized.
- `children` that is not a plain string — fall through to default rendering.
- Short-form wikis generated as `owner/repo` with no `repo_url` (so `repoInfo.repoUrl` is null and
  not `local`): `buildBlobUrl` returns `null` → citation renders as plain text. Acceptable silent
  degradation; most wikis here carry a real `repoUrl` in the cache.
- An existing cached page generated before the `.git` fix still has `…repo.git/-/blob/main/FILE`
  in its `<details>` block. `extractDefaultBranch` tolerates this (the branch segment still parses
  to `main`), so branch detection is unaffected.

## Testing

- **Foundational test first** (de-risks the whole render-time approach): assert that rendering
  `Sources: [CAL101.txt:51-54]()` through `<Markdown>` produces an `<a>` element whose `href` is
  empty/falsy and whose text is `CAL101.txt:51-54`. This confirms react-markdown emits a link node
  for the empty-destination `[text]()` (guaranteed by CommonMark, but verified explicitly here).
  If this fails, the render-time approach is invalid — stop and reconsider.
- `vitest` dev dependency + minimal config. Unit tests for `citationUrl.ts`:
  - `parseCitation`: range, single line, whole file, path with `/`, non-citation → null.
  - `buildBlobUrl`: gitlab, github, bitbucket; `.git` stripped; trailing-slash normalized; local → null.
  - `lineAnchor`: each provider, range vs single vs none.
  - `extractDefaultBranch`: gitlab/github/bitbucket URL present; absent → `'main'`.
  - `buildCitationHref`: end-to-end happy path; local → null.
- Manual verification in the running app: open a generated page, click a `Sources:` citation,
  confirm it opens the correct GitLab blob URL at the cited lines; confirm a local-repo wiki shows
  plain text.
