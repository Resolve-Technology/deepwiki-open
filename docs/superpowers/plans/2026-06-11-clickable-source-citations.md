# Clickable Source Citations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AI-generated `Sources: [file:lines]()` citations clickable, opening the exact lines in the repo's web UI (GitLab/GitHub), at render time so every existing wiki benefits with no regeneration.

**Architecture:** A pure util (`src/utils/citationUrl.ts`) parses a citation's link text and builds a repo blob URL + line anchor. `src/components/Markdown.tsx`'s `<a>` renderer uses it: empty-href links whose text is a citation become real links (or plain text for local repos); real links are untouched. The repo's default branch is extracted from the blob URLs already present in each page's `<details>` block. Separately, the `.git` URL bug in the Python `generate_file_url` is fixed.

**Tech Stack:** TypeScript, React 19, react-markdown v10, Next.js 15, Vitest (new), Python/pytest (backend fix).

---

## File Structure

- **Create** `src/utils/citationUrl.ts` — pure citation-parsing + URL-building functions.
- **Create** `src/utils/citationUrl.test.ts` — unit tests for the util (Vitest, node env).
- **Create** `src/utils/reactMarkdownEmptyHref.test.tsx` — foundational test confirming react-markdown emits a link node for `[text]()`.
- **Create** `vitest.config.ts` — minimal Vitest config with the `@` alias and React plugin.
- **Modify** `package.json` — add `test` script + `vitest`/`@vitejs/plugin-react` devDeps.
- **Modify** `src/components/Markdown.tsx` — add optional `repoInfo` prop, branch memo, rewrite the `a` renderer.
- **Modify** `src/app/[owner]/[repo]/page.tsx:1162` — pass `repoInfo={effectiveRepoInfo}`.
- **Modify** `api/wiki_prompts.py` (`generate_file_url`, ~lines 31-49) — strip trailing `.git`.
- **Create** `tests/unit/test_generate_file_url.py` — pytest for the `.git` fix.

---

## Task 1: Vitest tooling + foundational react-markdown check

**Files:**
- Modify: `package.json`
- Create: `vitest.config.ts`
- Create: `src/utils/reactMarkdownEmptyHref.test.tsx`

- [ ] **Step 1: Install dev dependencies**

Run:
```bash
yarn add -D vitest @vitejs/plugin-react
```
Expected: `vitest` and `@vitejs/plugin-react` appear under `devDependencies` in `package.json`.

- [ ] **Step 2: Add the test script to `package.json`**

In the `"scripts"` block, add a `test` entry so it reads:
```json
  "scripts": {
    "dev": "next dev --turbopack --port 3000",
    "build": "next build",
    "start": "next start",
    "lint": "next lint",
    "test": "vitest run"
  },
```

- [ ] **Step 3: Create `vitest.config.ts`**

```ts
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'node',
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
});
```

- [ ] **Step 4: Write the foundational failing test**

This confirms the riskiest assumption: that react-markdown turns `[text]()` (empty destination) into a link element. If this fails, STOP — the render-time approach is invalid.

Create `src/utils/reactMarkdownEmptyHref.test.tsx`:
```tsx
import { describe, it, expect } from 'vitest';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ReactMarkdown from 'react-markdown';

describe('react-markdown empty-href links', () => {
  it('renders [text]() as an <a> element carrying the text', () => {
    const html = renderToStaticMarkup(
      React.createElement(ReactMarkdown, null, '[CAL101.txt:51-54]()'),
    );
    // A link element is produced...
    expect(html).toMatch(/<a\b/);
    // ...and the citation text is preserved as its label.
    expect(html).toContain('CAL101.txt:51-54');
  });
});
```

- [ ] **Step 5: Run the test**

Run: `yarn test src/utils/reactMarkdownEmptyHref.test.tsx`
Expected: PASS. If it FAILS (no `<a>` produced), stop and revisit the design — do not continue.

- [ ] **Step 6: Commit**

```bash
git add package.json yarn.lock vitest.config.ts src/utils/reactMarkdownEmptyHref.test.tsx
git commit -m "test: add vitest + foundational react-markdown empty-href check"
```

---

## Task 2: `citationUrl.ts` util (TDD)

**Files:**
- Create: `src/utils/citationUrl.ts`
- Test: `src/utils/citationUrl.test.ts`

- [ ] **Step 1: Write the failing tests**

Create `src/utils/citationUrl.test.ts`:
```ts
import { describe, it, expect } from 'vitest';
import {
  parseCitation,
  buildBlobUrl,
  lineAnchor,
  extractDefaultBranch,
  buildCitationHref,
} from '@/utils/citationUrl';
import RepoInfo from '@/types/repoinfo';

const gitlab: RepoInfo = {
  owner: 'poc', repo: 'code2_sqlcbl_cal101', type: 'gitlab', token: null,
  localPath: null, repoUrl: 'https://gitlab.reslv.one/poc/code2_sqlcbl_cal101.git',
};
const github: RepoInfo = {
  owner: 'o', repo: 'r', type: 'github', token: null, localPath: null,
  repoUrl: 'https://github.com/o/r',
};
const local: RepoInfo = {
  owner: 'local', repo: 'x', type: 'local', token: null,
  localPath: '/root/.adalflow/repos/x', repoUrl: null,
};

describe('parseCitation', () => {
  it('parses a line range', () => {
    expect(parseCitation('CAL101.txt:51-54')).toEqual({ filePath: 'CAL101.txt', startLine: 51, endLine: 54 });
  });
  it('parses a single line', () => {
    expect(parseCitation('copybook/CLNMSKM.txt:12')).toEqual({ filePath: 'copybook/CLNMSKM.txt', startLine: 12 });
  });
  it('parses a whole-file citation', () => {
    expect(parseCitation('README.md')).toEqual({ filePath: 'README.md' });
  });
  it('returns null for non-citation text', () => {
    expect(parseCitation('see the overview page')).toBeNull();
  });
});

describe('buildBlobUrl', () => {
  it('builds a gitlab URL and strips .git', () => {
    expect(buildBlobUrl(gitlab, 'CAL101.txt', 'main'))
      .toBe('https://gitlab.reslv.one/poc/code2_sqlcbl_cal101/-/blob/main/CAL101.txt');
  });
  it('builds a github URL', () => {
    expect(buildBlobUrl(github, 'src/a.ts', 'develop'))
      .toBe('https://github.com/o/r/blob/develop/src/a.ts');
  });
  it('returns null for a local repo', () => {
    expect(buildBlobUrl(local, 'CAL101.txt', 'main')).toBeNull();
  });
  it('returns null when repoUrl is missing', () => {
    expect(buildBlobUrl({ ...github, repoUrl: null }, 'a.ts', 'main')).toBeNull();
  });
});

describe('lineAnchor', () => {
  it('github range uses L on both ends', () => {
    expect(lineAnchor('github', 51, 54)).toBe('#L51-L54');
  });
  it('gitlab range omits L on the end', () => {
    expect(lineAnchor('gitlab', 51, 54)).toBe('#L51-54');
  });
  it('single line is #L<n>', () => {
    expect(lineAnchor('gitlab', 51)).toBe('#L51');
    expect(lineAnchor('github', 51)).toBe('#L51');
  });
  it('no anchor without a start line', () => {
    expect(lineAnchor('gitlab')).toBe('');
  });
  it('bitbucket and unknown types get no line anchor', () => {
    expect(lineAnchor('bitbucket', 51, 54)).toBe('');
  });
});

describe('extractDefaultBranch', () => {
  it('reads the branch from a gitlab blob URL', () => {
    expect(extractDefaultBranch('x [f](https://gitlab.reslv.one/p/r/-/blob/release/CAL101.txt) y'))
      .toBe('release');
  });
  it('reads the branch from a github blob URL', () => {
    expect(extractDefaultBranch('[f](https://github.com/o/r/blob/main/a.ts)')).toBe('main');
  });
  it('tolerates a .git-suffixed URL (pre-fix cached pages)', () => {
    expect(extractDefaultBranch('[f](https://gitlab.reslv.one/p/r.git/-/blob/main/a.txt)')).toBe('main');
  });
  it('falls back to main when no blob URL is present', () => {
    expect(extractDefaultBranch('no links here')).toBe('main');
  });
});

describe('buildCitationHref', () => {
  it('composes URL + anchor for gitlab', () => {
    expect(buildCitationHref(gitlab, 'main', 'CAL101.txt:51-54'))
      .toBe('https://gitlab.reslv.one/poc/code2_sqlcbl_cal101/-/blob/main/CAL101.txt#L51-54');
  });
  it('returns null for a local repo', () => {
    expect(buildCitationHref(local, 'main', 'CAL101.txt:51-54')).toBeNull();
  });
  it('returns null for non-citation text', () => {
    expect(buildCitationHref(gitlab, 'main', 'just words')).toBeNull();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `yarn test src/utils/citationUrl.test.ts`
Expected: FAIL — `Failed to resolve import "@/utils/citationUrl"` (file doesn't exist yet).

- [ ] **Step 3: Implement `src/utils/citationUrl.ts`**

```ts
import RepoInfo from '@/types/repoinfo';

export interface ParsedCitation {
  filePath: string;
  startLine?: number;
  endLine?: number;
}

// "path/to/file.ext" with an optional ":12" or ":12-34" suffix.
// Requires a file extension so ordinary link labels don't match.
const CITATION_RE = /^(.+?\.[A-Za-z0-9]+)(?::(\d+)(?:-(\d+))?)?$/;

export function parseCitation(text: string): ParsedCitation | null {
  const m = CITATION_RE.exec(text.trim());
  if (!m) return null;
  const [, filePath, start, end] = m;
  const out: ParsedCitation = { filePath };
  if (start) out.startLine = parseInt(start, 10);
  if (end) out.endLine = parseInt(end, 10);
  return out;
}

export function buildBlobUrl(repoInfo: RepoInfo, filePath: string, branch: string): string | null {
  if (repoInfo.type === 'local' || !repoInfo.repoUrl) return null;
  const base = repoInfo.repoUrl.replace(/\.git$/, '').replace(/\/+$/, '');
  switch (repoInfo.type) {
    case 'github': return `${base}/blob/${branch}/${filePath}`;
    case 'gitlab': return `${base}/-/blob/${branch}/${filePath}`;
    case 'bitbucket': return `${base}/src/${branch}/${filePath}`;
    default: return null;
  }
}

export function lineAnchor(repoType: string, start?: number, end?: number): string {
  if (!start) return '';
  const range = end && end !== start;
  switch (repoType) {
    case 'github': return range ? `#L${start}-L${end}` : `#L${start}`;
    case 'gitlab': return range ? `#L${start}-${end}` : `#L${start}`;
    // bitbucket + unknown: file-level link only (anchor format unverified)
    default: return '';
  }
}

const BRANCH_RE = /\/(?:-\/blob|blob|src)\/([^/#?]+)\//;

export function extractDefaultBranch(content: string): string {
  const m = BRANCH_RE.exec(content);
  return m ? m[1] : 'main';
}

export function buildCitationHref(repoInfo: RepoInfo, branch: string, text: string): string | null {
  const cite = parseCitation(text);
  if (!cite) return null;
  const url = buildBlobUrl(repoInfo, cite.filePath, branch);
  if (!url) return null;
  return url + lineAnchor(repoInfo.type, cite.startLine, cite.endLine);
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `yarn test src/utils/citationUrl.test.ts`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
git add src/utils/citationUrl.ts src/utils/citationUrl.test.ts
git commit -m "feat: add citationUrl util for parsing and building source-citation links"
```

---

## Task 3: Wire the util into `Markdown.tsx`

**Files:**
- Modify: `src/components/Markdown.tsx`

- [ ] **Step 1: Add imports**

At the top of `src/components/Markdown.tsx`, after the existing imports (the `import 'katex/dist/katex.min.css';` line at line 10), add:
```tsx
import RepoInfo from '@/types/repoinfo';
import { parseCitation, buildBlobUrl, lineAnchor, extractDefaultBranch } from '@/utils/citationUrl';
```

- [ ] **Step 2: Add the optional prop**

Change the `MarkdownProps` interface (currently lines 12-14):
```tsx
interface MarkdownProps {
  content: string;
  repoInfo?: RepoInfo;
}
```
And the component signature (currently line 16):
```tsx
const Markdown: React.FC<MarkdownProps> = ({ content, repoInfo }) => {
```

- [ ] **Step 3: Compute the default branch once**

Immediately inside the component body, before `const MarkdownComponents` (line 17), add:
```tsx
  const defaultBranch = React.useMemo(() => extractDefaultBranch(content), [content]);
```

- [ ] **Step 4: Replace the `a` renderer**

Replace the entire existing `a({ children, href, ...props }) { ... }` block (currently lines 63-75) with:
```tsx
    a({ children, href, ...props }: { children?: React.ReactNode; href?: string }) {
      const linkClass = "text-purple-600 dark:text-purple-400 hover:underline font-medium";

      // Real link (e.g. the top-of-page <details> blob links) — leave untouched.
      if (href) {
        return (
          <a href={href} className={linkClass} target="_blank" rel="noopener noreferrer" {...props}>
            {children}
          </a>
        );
      }

      // Empty href: maybe a "Sources" citation. Get the plain text label.
      const text =
        typeof children === 'string'
          ? children
          : Array.isArray(children) && children.every((c) => typeof c === 'string')
            ? (children as string[]).join('')
            : null;

      const cite = text ? parseCitation(text) : null;
      if (text && cite && repoInfo) {
        const url = buildBlobUrl(repoInfo, cite.filePath, defaultBranch);
        if (url) {
          const finalHref = url + lineAnchor(repoInfo.type, cite.startLine, cite.endLine);
          return (
            <a href={finalHref} className={linkClass} target="_blank" rel="noopener noreferrer" {...props}>
              {children}
            </a>
          );
        }
        // Citation, but no buildable URL (local repo / no repoUrl) → plain text, not a dead link.
        return (
          <span className="text-gray-500 dark:text-gray-400 font-medium" {...props}>
            {children}
          </span>
        );
      }

      // Not a citation (or unstringifiable) → preserve previous behavior.
      return (
        <a href={href} className={linkClass} target="_blank" rel="noopener noreferrer" {...props}>
          {children}
        </a>
      );
    },
```

- [ ] **Step 5: Verify the build compiles**

Run: `yarn lint && yarn test`
Expected: lint passes; existing tests still PASS. (No new unit test here — the `a` renderer pulls in heavy browser-only imports via `Markdown.tsx`; it is covered by the util tests plus the manual verification in Task 6.)

- [ ] **Step 6: Commit**

```bash
git add src/components/Markdown.tsx
git commit -m "feat: render Sources citations as clickable blob links in Markdown"
```

---

## Task 4: Pass `repoInfo` from the wiki page

**Files:**
- Modify: `src/app/[owner]/[repo]/page.tsx` (the `<Markdown>` call at line 1162)

- [ ] **Step 1: Add the prop**

The current call reads:
```tsx
                    <Markdown
                      content={generatedPages[currentPageId].content}
                    />
```
Change it to:
```tsx
                    <Markdown
                      content={generatedPages[currentPageId].content}
                      repoInfo={effectiveRepoInfo}
                    />
```
(`effectiveRepoInfo` is already in scope — it's used throughout this component, e.g. lines 281-283, 627.)

- [ ] **Step 2: Verify it compiles**

Run: `yarn lint`
Expected: PASS, no type errors (`effectiveRepoInfo` is a `RepoInfo`).

- [ ] **Step 3: Commit**

```bash
git add "src/app/[owner]/[repo]/page.tsx"
git commit -m "feat: pass repoInfo to Markdown so citations link to source"
```

---

## Task 5: Fix the `.git` bug in `generate_file_url` (backend)

**Files:**
- Modify: `api/wiki_prompts.py` (`generate_file_url`, ~lines 31-49)
- Test: `tests/unit/test_generate_file_url.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_generate_file_url.py`:
```python
"""Tests for blob-URL construction (the .git-stripping fix)."""
from api.wiki_prompts import generate_file_url


def test_gitlab_strips_dot_git():
    url = generate_file_url(
        "https://gitlab.reslv.one/poc/code2_sqlcbl_cal101.git",
        "gitlab", "CAL101.txt", "main")
    assert url == "https://gitlab.reslv.one/poc/code2_sqlcbl_cal101/-/blob/main/CAL101.txt"


def test_github_without_dot_git_unchanged():
    url = generate_file_url("https://github.com/o/r", "github", "a.ts", "main")
    assert url == "https://github.com/o/r/blob/main/a.ts"


def test_local_returns_bare_path():
    assert generate_file_url("", "local", "CAL101.txt", "main") == "CAL101.txt"
```

- [ ] **Step 2: Run the test to verify the gitlab case fails**

Run: `python -m pytest tests/unit/test_generate_file_url.py -v`
Expected: `test_gitlab_strips_dot_git` FAILS — current output contains `code2_sqlcbl_cal101.git/-/blob/...`. The other two PASS.

- [ ] **Step 3: Implement the fix**

In `api/wiki_prompts.py`, replace the body of `generate_file_url` (the part after the `local`/empty guards) so the URL base has any trailing `.git` and slash stripped:
```python
def generate_file_url(repo_url: str, repo_type: str, file_path: str,
                      default_branch: str) -> str:
    """Port of page.tsx generateFileUrl (github blob / gitlab -/blob / bitbucket src)."""
    if repo_type == "local":
        return file_path
    if not repo_url:
        return file_path
    try:
        base = repo_url.rstrip("/")
        if base.endswith(".git"):
            base = base[:-len(".git")]
        # Detect by hostname substrings, same as TS
        if "github" in base:
            return f"{base}/blob/{default_branch}/{file_path}"
        elif "gitlab" in base:
            return f"{base}/-/blob/{default_branch}/{file_path}"
        elif "bitbucket" in base:
            return f"{base}/src/{default_branch}/{file_path}"
    except Exception:
        pass
    return file_path
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_generate_file_url.py -v`
Expected: all three PASS.

- [ ] **Step 5: Commit**

```bash
git add api/wiki_prompts.py tests/unit/test_generate_file_url.py
git commit -m "fix: strip trailing .git when building blob URLs in generate_file_url"
```

---

## Task 6: End-to-end verification

**Files:** none (verification only). Use the `verify` skill if desired.

- [ ] **Step 1: Run the full JS test suite**

Run: `yarn test`
Expected: all tests PASS.

- [ ] **Step 2: Run the backend test**

Run: `python -m pytest tests/unit/test_generate_file_url.py -v`
Expected: all PASS.

- [ ] **Step 3: Rebuild and restart the app**

The frontend source is not volume-mounted; a code change needs a rebuild. First bump `src/version.ts` `APP_VERSION` (project convention), then:
```bash
docker compose up -d --build
```
Expected: container rebuilds and starts healthy.

- [ ] **Step 4: Manually verify in the browser**

Open an existing GitLab-backed wiki page (e.g. `poc/code2_sqlcbl_cal101`). Find a `Sources: [CAL101.txt:51-54]` citation and click it.
Expected: opens `https://gitlab.reslv.one/poc/code2_sqlcbl_cal101/-/blob/<branch>/CAL101.txt#L51-54` in a new tab, scrolled/highlighted to those lines. Confirm the URL has **no** `.git` and the line anchor is present.

- [ ] **Step 5: Verify the local-repo case**

Open a `local`-type wiki page (e.g. one generated from `/root/.adalflow/repos/...`). Confirm `Sources:` citations render as plain grey text (not a dead clickable link).

- [ ] **Step 6: Spot-check the GitLab range anchor**

On the opened GitLab page, confirm `#L51-54` actually highlights lines 51–54. If GitLab ignores it, the correct form may differ — if so, adjust `lineAnchor`'s `gitlab` case and re-run `yarn test`.

- [ ] **Step 7: Final completion check**

Use the `superpowers:verification-before-completion` skill: confirm all checkboxes are done, both test suites pass, and the manual checks above succeeded before declaring the feature complete.

---

## Notes for the implementer

- **DRY:** the `linkClass` string is defined once inside the `a` renderer; reuse it.
- **YAGNI:** do not thread `repoInfo` into the Ask/Workshop/WikiReview `<Markdown>` call sites — the prop is optional and those contexts are out of scope.
- **The Python fix only affects newly-generated wikis.** Existing cached pages keep their `.git` `<details>` links until regenerated; the frontend citation links are correct regardless because they're built at render time from `repoInfo.repoUrl` (with `.git` stripped in `buildBlobUrl`).
