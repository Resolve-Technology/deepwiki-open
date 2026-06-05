# Self-Correcting Generation + Apply-Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) First-time wiki generation gets a per-page self-review pass — a fresh request to the same LLM verifies each page against the repo code and corrects it (toggleable, default on). (2) Stored Model Reviews get an "Apply to wiki" button: the wiki's own model classifies which pages the review affects, the user confirms, the affected pages are revised and saved.

**Architecture:** All orchestration is frontend-side like the existing generation flow. A new `src/utils/wikiRevision.ts` holds the shared promise-wrapped websocket call, the prompts, and the safety parser (`NO_CHANGES` / `Error:` / <30%-length guards so a bad correction never destroys a good page). Corrections get repo-code context via the existing `rag_query` mechanism. The only backend change is an optional `self_reviewed` passthrough field on the wiki cache models.

**Tech Stack:** Next.js/React/TypeScript frontend (`src/`), FastAPI backend (`api/api.py`), pytest (`tests/unit/`, run via `.venv/bin/python`).

**Spec:** `docs/superpowers/specs/2026-06-05-self-review-and-apply-review-design.md`

**Conventions:**
- Run all commands from `/home/ubuntu/deepwiki-open`.
- Backend tests: `PYTHONPATH=/home/ubuntu/deepwiki-open .venv/bin/python -m pytest tests/unit/test_wiki_cache_versions.py -v` (pytest.ini's `testpaths` is `test`, so the path must be explicit).
- Frontend build: host has no node — run `docker run --rm -v /home/ubuntu/deepwiki-open:/app -w /app node:20-alpine sh -c "npm run build"`; afterwards restore the lockfile if the container touched it: `git checkout -- yarn.lock package-lock.json 2>/dev/null; true`. Never claim the build passed without running it.
- Line numbers below are approximate (the file has churned); locate code by the quoted search strings.

---

### Task 1: Shared revision utilities (`src/utils/wikiRevision.ts`)

**Files:**
- Create: `src/utils/wikiRevision.ts`

- [ ] **Step 1: Create the file with this exact content**

```typescript
/**
 * Shared plumbing for LLM-driven wiki revision passes:
 *  - the self-review pass during first-time generation, and
 *  - applying a stored Model Review back onto the wiki content.
 *
 * Both run one-shot chat requests over the existing websocket pipeline and
 * must never destroy a good page: parseRevisedContent falls back to the
 * original on NO_CHANGES, backend error text, or suspiciously short output.
 */
import { createChatWebSocket, closeWebSocket, ChatCompletionRequest } from './websocketClient';
import { WikiPage } from '@/types/wiki/wikipage';

export const NO_CHANGES_TOKEN = 'NO_CHANGES';

/** Runs a single chat request to completion and resolves with the full text. */
export function runChatOnce(
  request: ChatCompletionRequest,
  timeoutMs: number = 600_000,
): Promise<string> {
  return new Promise((resolve, reject) => {
    let content = '';
    let settled = false;
    const finish = (fn: () => void) => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        fn();
      }
    };
    const timer = setTimeout(() => {
      finish(() => {
        closeWebSocket(ws);
        reject(new Error('Chat request timed out'));
      });
    }, timeoutMs);
    const ws = createChatWebSocket(
      request,
      (message: string) => {
        content += message;
      },
      () => finish(() => reject(new Error('WebSocket error during chat request'))),
      () =>
        finish(() => {
          const trimmed = content.trim();
          if (!trimmed) {
            reject(new Error('Empty response from model'));
          } else if (trimmed.startsWith('Error:')) {
            // The backend reports failures as plain text on the stream.
            reject(new Error(trimmed.slice(0, 300)));
          } else {
            resolve(content);
          }
        }),
    );
  });
}

/**
 * Short retrieval query so revision prompts get repo code context via RAG
 * even though the full prompt exceeds the websocket's large-input gate.
 */
export function buildPageRagQuery(page: WikiPage): string {
  const files = page.filePaths.slice(0, 30).join(', ');
  return `Source code relevant to documentation page "${page.title}". Key files: ${files}`.slice(0, 4000);
}

export function buildSelfReviewPrompt(page: WikiPage, repoUrl: string): string {
  return `You are reviewing a documentation page that was just generated for the repository ${repoUrl}. You have access to the repository's actual source code through the provided context — verify the page against it with fresh eyes.

Correct any factual errors: wrong claims about behavior, invented functions/APIs/files, incorrect file references, broken mermaid syntax, or missing critical caveats. Keep the page's original structure, level of detail, and language.

If the page is accurate as written, reply with exactly: ${NO_CHANGES_TOKEN}
Otherwise reply with the COMPLETE corrected page in markdown — no preamble, no explanation of what you changed, no code fence around the whole page.

<page title="${page.title}" files="${page.filePaths.join(', ')}">
${page.content}
</page>`;
}

export function buildAffectedPagesPrompt(reviewContent: string, pages: WikiPage[]): string {
  return `A reviewer critiqued a documentation wiki. Decide which of the wiki's pages need CONTENT CHANGES based on the review below.

Wiki pages:
${pages.map(p => `- ${p.title}`).join('\n')}

<review>
${reviewContent}
</review>

Reply with ONLY the exact titles of pages that need changes, one per line, copied verbatim from the list above. If no page needs changes, reply with exactly: NONE`;
}

/** Matches response lines against known page titles (case-insensitive, exact). */
export function parseAffectedPages(response: string, pages: WikiPage[]): WikiPage[] {
  const trimmed = response.trim();
  if (!trimmed || trimmed.toUpperCase() === 'NONE') return [];
  const byTitle = new Map(pages.map(p => [p.title.trim().toLowerCase(), p]));
  const seen = new Set<string>();
  const affected: WikiPage[] = [];
  for (const line of trimmed.split('\n')) {
    const key = line.replace(/^[-*\d.\s]+/, '').trim().toLowerCase();
    const page = byTitle.get(key);
    if (page && !seen.has(page.id)) {
      seen.add(page.id);
      affected.push(page);
    }
  }
  return affected;
}

export function buildApplyReviewPrompt(page: WikiPage, reviewContent: string, repoUrl: string): string {
  return `You are revising a documentation page for the repository ${repoUrl} based on reviewer feedback. You have access to the repository's actual source code through the provided context — verify each piece of feedback against the code before applying it; ignore feedback that is incorrect or concerns other pages. Keep the page's original structure, level of detail, and language.

If no changes to THIS page are warranted, reply with exactly: ${NO_CHANGES_TOKEN}
Otherwise reply with the COMPLETE revised page in markdown — no preamble, no explanation of what you changed, no code fence around the whole page.

<review>
${reviewContent}
</review>

<page title="${page.title}" files="${page.filePaths.join(', ')}">
${page.content}
</page>`;
}

/**
 * Safety gate for revision responses. Returns the original unchanged when the
 * model said NO_CHANGES, the backend streamed an error, or the output is
 * suspiciously short (truncated/refused corrections must not destroy pages).
 */
export function parseRevisedContent(
  original: string,
  response: string,
): { content: string; changed: boolean } {
  let cleaned = response.trim();
  if (!cleaned) return { content: original, changed: false };
  // Strip a whole-page markdown fence like the generation path does.
  cleaned = cleaned.replace(/^```(?:markdown)?\s*/i, '').replace(/```\s*$/i, '').trim();
  if (!cleaned || cleaned === NO_CHANGES_TOKEN || cleaned.startsWith('Error:')) {
    return { content: original, changed: false };
  }
  if (cleaned.length < original.length * 0.3) {
    return { content: original, changed: false };
  }
  if (cleaned === original.trim()) {
    return { content: original, changed: false };
  }
  return { content: cleaned, changed: true };
}
```

- [ ] **Step 2: Commit**

```bash
git add src/utils/wikiRevision.ts
git commit -m "feat: shared utilities for LLM wiki revision passes"
```

---

### Task 2: Backend — `self_reviewed` metadata passthrough

**Files:**
- Modify: `api/api.py` (`WikiCacheData`, `WikiCacheRequest`, `save_wiki_cache`)
- Test (append only): `tests/unit/test_wiki_cache_versions.py`

- [ ] **Step 1: Append the failing test**

```python
def test_self_reviewed_flag_roundtrip(cache_dir):
    req = make_cache_request("claude", "claude-sonnet-4-6")
    req.self_reviewed = True
    asyncio.run(save_wiki_cache(req))
    data = asyncio.run(read_wiki_cache("owner", "repo", "github", "en",
                                       provider="claude", model="claude-sonnet-4-6"))
    assert data.self_reviewed is True
```

Run `PYTHONPATH=/home/ubuntu/deepwiki-open .venv/bin/python -m pytest tests/unit/test_wiki_cache_versions.py -v -k self_reviewed` — expect failure (`object has no field "self_reviewed"`).

- [ ] **Step 2: Add the field to both models and the save payload**

In `WikiCacheData`, after `repo_commit`:

```python
    self_reviewed: Optional[bool] = None  # pages went through the self-review pass
```

In `WikiCacheRequest`, after `model`:

```python
    self_reviewed: Optional[bool] = None
```

In `save_wiki_cache`, add to the `WikiCacheData(...)` payload construction:

```python
            self_reviewed=data.self_reviewed,
```

- [ ] **Step 3: Run the full backend test file — all pass (35 + new = 36 in this file; also run the legacy `test/` dir).**

- [ ] **Step 4: Commit**

```bash
git add api/api.py tests/unit/test_wiki_cache_versions.py
git commit -m "feat: self_reviewed metadata on wiki cache"
```

---

### Task 3: Self-review pass during generation (page.tsx + ModelSelectionModal)

**Files:**
- Modify: `src/app/[owner]/[repo]/page.tsx`
- Modify: `src/components/ModelSelectionModal.tsx`

- [ ] **Step 1: Toggle state in page.tsx**

(a) Near `const isComprehensiveParam = searchParams.get('comprehensive') !== 'false';` add:

```typescript
  const isSelfReviewParam = searchParams.get('self_review') !== 'false';
```

(b) Find where `isComprehensiveView` state is declared (`useState(isComprehensiveParam)`) and add next to it:

```typescript
  const [isSelfReviewEnabled, setIsSelfReviewEnabled] = useState(isSelfReviewParam);
```

(c) Add the import:

```typescript
import { runChatOnce, buildPageRagQuery, buildSelfReviewPrompt, parseRevisedContent } from '@/utils/wikiRevision';
```

- [ ] **Step 2: Insert the correction pass at the completion point**

In `generatePageContent`, locate the completion point (search for `// Clean up markdown delimiters`) — both the WS and HTTP-fallback paths converge there. Immediately AFTER the existing fence-strip line and the `console.log("Received content for ...")` line, and BEFORE `// Store the FINAL generated content`, insert:

```typescript
        // Self-review pass: a fresh request to the same model verifies the page
        // against the repo code (RAG via rag_query) and corrects it. Toggleable;
        // any failure keeps the original content.
        if (isSelfReviewEnabled && content.trim() && !content.startsWith('Error')) {
          setLoadingMessage(`Reviewing ${page.title} against the codebase...`);
          try {
            const reviewBody: Record<string, any> = {  // eslint-disable-line @typescript-eslint/no-explicit-any
              repo_url: repoUrl,
              type: effectiveRepoInfo.type,
              messages: [{
                role: 'user',
                content: buildSelfReviewPrompt({ ...page, content }, repoUrl),
              }],
              rag_query: buildPageRagQuery(page),
            };
            addTokensToRequestBody(reviewBody, currentToken, effectiveRepoInfo.type, selectedProviderState, selectedModelState, isCustomSelectedModelState, customSelectedModelState, language, modelExcludedDirs, modelExcludedFiles, modelIncludedDirs, modelIncludedFiles);
            const reviewed = await runChatOnce(reviewBody as ChatCompletionRequest);
            const { content: corrected, changed } = parseRevisedContent(content, reviewed);
            if (changed) {
              console.log(`Self-review corrected ${page.title} (${content.length} -> ${corrected.length} chars)`);
              content = corrected;
            } else {
              console.log(`Self-review: no changes for ${page.title}`);
            }
          } catch (reviewErr) {
            console.warn(`Self-review failed for ${page.title}, keeping original:`, reviewErr);
          }
        }
```

Notes for the implementer:
- `repoUrl` is already in scope in `generatePageContent` (it builds `requestBody.repo_url`); if the local name differs, use that variable.
- Import `ChatCompletionRequest` from `@/utils/websocketClient` if not already imported in page.tsx; if a type-cast friction arises (the request body is a loose Record), `as unknown as ChatCompletionRequest` is acceptable — match the file's existing looseness.
- Add `isSelfReviewEnabled` to `generatePageContent`'s dependency array.

- [ ] **Step 3: Carry the flag into the cache metadata**

(a) In the `saveCache` effect's `dataToCache` (search `provider: selectedProviderState`), add:

```typescript
              self_reviewed: isSelfReviewEnabled,
```

and add `isSelfReviewEnabled` to that effect's dependency array.

(b) In `wikiMeta` state, extend the type and the setters:

```typescript
  const [wikiMeta, setWikiMeta] = useState<{ generatedAt?: string; repoCommit?: string; selfReviewed?: boolean }>({});
```

- cache-hit path (search `setWikiMeta({ generatedAt: cachedData.generated_at`): add `selfReviewed: cachedData.self_reviewed`.
- save-success path (search `setWikiMeta({ generatedAt: result.generated_at`): add `selfReviewed: isSelfReviewEnabled`.

(c) In the "Generated by" line under the page title (search `Generated by {selectedProviderState}`), extend:

```tsx
                      Generated by {selectedProviderState}/{selectedModelState}
                      {wikiMeta.selfReviewed ? ' · self-reviewed' : ''}
                      {wikiMeta.generatedAt ? ` · ${new Date(wikiMeta.generatedAt).toLocaleString()}` : ''}
```

- [ ] **Step 4: Checkbox in ModelSelectionModal**

(a) Props (after `setIsComprehensiveView`):

```typescript
  isSelfReviewEnabled?: boolean;
  setIsSelfReviewEnabled?: (value: boolean) => void;
```

Destructure both with defaults (`isSelfReviewEnabled = true`).

(b) Local state mirroring the other fields:

```typescript
  const [localIsSelfReviewEnabled, setLocalIsSelfReviewEnabled] = useState(isSelfReviewEnabled);
```

Reset it in the `isOpen` effect (`setLocalIsSelfReviewEnabled(isSelfReviewEnabled);` — add `isSelfReviewEnabled` to that effect's deps), and commit it in `commitSelections`:

```typescript
    if (setIsSelfReviewEnabled) setIsSelfReviewEnabled(localIsSelfReviewEnabled);
```

(c) Render the checkbox right below the `WikiTypeSelector` (inside the same `showWikiType &&` guard so the Ask modal never shows it):

```tsx
            {showWikiType && (
              <label className="mt-3 flex items-center gap-2 text-sm text-[var(--foreground)] cursor-pointer">
                <input
                  type="checkbox"
                  checked={localIsSelfReviewEnabled}
                  onChange={(e) => setLocalIsSelfReviewEnabled(e.target.checked)}
                  className="accent-[var(--accent-primary)]"
                />
                Self-review pages against code (second pass, ~2x tokens)
              </label>
            )}
```

(d) In page.tsx's `<ModelSelectionModal ...>` usage, pass:

```tsx
        isSelfReviewEnabled={isSelfReviewEnabled}
        setIsSelfReviewEnabled={setIsSelfReviewEnabled}
```

- [ ] **Step 5: Build (Docker, per conventions) — must pass. Commit:**

```bash
git add "src/app/[owner]/[repo]/page.tsx" src/components/ModelSelectionModal.tsx
git commit -m "feat: per-page self-review pass during wiki generation (toggleable)"
```

---

### Task 4: Apply-review flow (WikiReviewModal + page.tsx)

**Files:**
- Modify: `src/components/WikiReviewModal.tsx`
- Modify: `src/app/[owner]/[repo]/page.tsx`

- [ ] **Step 1: New props and state in WikiReviewModal**

(a) Props — add to `WikiReviewModalProps`:

```typescript
  /** Receives pages whose content was revised by applying a review. */
  onPagesRevised?: (updated: Record<string, WikiPage>) => void;
```

(b) Imports:

```typescript
import { runChatOnce, buildPageRagQuery, buildAffectedPagesPrompt, parseAffectedPages, buildApplyReviewPrompt, parseRevisedContent } from '@/utils/wikiRevision';
```

(c) State (near `reviewContent`):

```typescript
  type ApplyPhase = 'idle' | 'classifying' | 'confirm' | 'revising' | 'done';
  const [applyPhase, setApplyPhase] = useState<ApplyPhase>('idle');
  const [applyTarget, setApplyTarget] = useState<WikiReview | null>(null);
  const [affectedPages, setAffectedPages] = useState<WikiPage[]>([]);
  const [applyProgress, setApplyProgress] = useState('');
  const [applySummary, setApplySummary] = useState('');
```

Reset all of these to their initial values in the existing close-on-`isOpen`-false effect.

- [ ] **Step 2: The apply pipeline**

Add inside the component:

```typescript
  const wikiRequestBase = useCallback((): Omit<ChatCompletionRequest, 'messages'> => ({
    repo_url: getRepoUrl(repoInfo),
    type: repoInfo.type,
    provider: reviewedProvider,
    model: reviewedModel,
    language: language,
    token: token,
  }), [repoInfo, reviewedProvider, reviewedModel, language, token]);

  // Phase 1: ask the wiki's own model which pages the review affects.
  const startApply = useCallback(async (review: WikiReview) => {
    setApplyTarget(review);
    setApplyPhase('classifying');
    setApplySummary('');
    try {
      const response = await runChatOnce({
        ...wikiRequestBase(),
        messages: [{ role: 'user', content: buildAffectedPagesPrompt(review.content, pages) }],
      });
      const affected = parseAffectedPages(response, pages);
      if (affected.length === 0) {
        setApplyPhase('done');
        setApplySummary('The review does not call for content changes to any page.');
        return;
      }
      setAffectedPages(affected);
      setApplyPhase('confirm');
    } catch (err) {
      setApplyPhase('idle');
      setReviewError(`Could not determine affected pages: ${err instanceof Error ? err.message : err}`);
    }
  }, [pages, wikiRequestBase]);

  // Phase 2 (after user confirmation): revise each affected page.
  const confirmApply = useCallback(async () => {
    if (!applyTarget) return;
    setApplyPhase('revising');
    const updated: Record<string, WikiPage> = {};
    let revised = 0, unchanged = 0, failed = 0;
    for (let i = 0; i < affectedPages.length; i++) {
      const page = affectedPages[i];
      setApplyProgress(`Revising ${page.title} (${i + 1}/${affectedPages.length})...`);
      try {
        const response = await runChatOnce({
          ...wikiRequestBase(),
          messages: [{ role: 'user', content: buildApplyReviewPrompt(page, applyTarget.content, getRepoUrl(repoInfo)) }],
          rag_query: buildPageRagQuery(page),
        });
        const { content, changed } = parseRevisedContent(page.content, response);
        if (changed) {
          updated[page.id] = { ...page, content };
          revised++;
        } else {
          unchanged++;
        }
      } catch (err) {
        console.warn(`Apply-review failed for ${page.title}, keeping original:`, err);
        failed++;
      }
    }
    if (Object.keys(updated).length > 0) {
      onPagesRevised?.(updated);
    }
    setApplyPhase('done');
    setApplyProgress('');
    setApplySummary(`Revised ${revised} page(s), ${unchanged} unchanged, ${failed} failed.`);
  }, [applyTarget, affectedPages, wikiRequestBase, repoInfo, onPagesRevised]);
```

- [ ] **Step 3: UI wiring in the modal body**

(a) An "Apply to wiki" button must appear (i) under a freshly streamed review (next to where `reviewContent` renders, once `!isReviewing`) and (ii) on each saved review row. Both call `startApply(review)`. Enablement guard — define once:

```typescript
  const canApply = (review: WikiReview) =>
    review.reviewed_provider === reviewedProvider && review.reviewed_model === reviewedModel &&
    pages.length > 0 && applyPhase === 'idle';
```

For the live review, construct the `WikiReview` object the same way `saveReview` does. Disabled buttons get `title="Load this review's wiki version first"` when the provider/model mismatch is the reason.

(b) Confirmation block (render when `applyPhase === 'confirm'`):

```tsx
            {applyPhase === 'confirm' && (
              <div className="mt-4 p-4 rounded-md border border-[var(--accent-primary)]/40">
                <p className="text-sm text-[var(--foreground)] mb-2">
                  This review affects {affectedPages.length} page(s). Revise them with {reviewedProvider}/{reviewedModel}?
                </p>
                <ul className="text-sm text-[var(--muted)] list-disc ml-5 mb-3">
                  {affectedPages.map(p => <li key={p.id}>{p.title}</li>)}
                </ul>
                <div className="flex gap-2">
                  <button type="button" onClick={confirmApply}
                    className="px-3 py-1.5 text-sm rounded-md bg-[var(--accent-primary)]/90 text-white hover:bg-[var(--accent-primary)]">
                    Apply changes
                  </button>
                  <button type="button" onClick={() => { setApplyPhase('idle'); setAffectedPages([]); }}
                    className="px-3 py-1.5 text-sm rounded-md border border-[var(--border-color)] text-[var(--muted)]">
                    Cancel
                  </button>
                </div>
              </div>
            )}
            {applyPhase === 'classifying' && <p className="mt-3 text-sm text-[var(--muted)]">Determining affected pages…</p>}
            {applyPhase === 'revising' && <p className="mt-3 text-sm text-[var(--muted)]">{applyProgress}</p>}
            {applyPhase === 'done' && applySummary && <p className="mt-3 text-sm text-[var(--foreground)]">{applySummary}</p>}
```

(c) While `applyPhase` is `classifying`/`revising`, disable the Start Review and Close-triggered re-apply paths (`disabled={isReviewing || applyPhase === 'classifying' || applyPhase === 'revising'}` on Start Review).

- [ ] **Step 4: page.tsx — receive and persist revised pages**

(a) Handler (place near `exportWiki`):

```typescript
  // Persist pages revised by applying a Model Review: merge into state and
  // save explicitly (the auto-save effect is gated off for cache-loaded wikis).
  const handlePagesRevised = useCallback(async (updated: Record<string, WikiPage>) => {
    const mergedPages = { ...generatedPages, ...updated };
    setGeneratedPages(mergedPages);
    if (!wikiStructure) return;
    try {
      const response = await fetch(`/api/wiki_cache`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          repo: effectiveRepoInfo,
          language: language,
          comprehensive: isComprehensiveView,
          wiki_structure: {
            ...wikiStructure,
            sections: wikiStructure.sections || [],
            rootSections: wikiStructure.rootSections || [],
          },
          generated_pages: mergedPages,
          provider: selectedProviderState,
          model: selectedModelState,
          self_reviewed: wikiMeta.selfReviewed,
        }),
      });
      if (response.ok) {
        const result = await response.json().catch(() => null);
        if (result) {
          setWikiMeta(prev => ({ ...prev, generatedAt: result.generated_at, repoCommit: result.repo_commit }));
        }
      } else {
        console.error('Failed to save revised wiki:', response.status, await response.text());
        setError('Revised pages are shown but could not be saved to the server cache.');
      }
    } catch (err) {
      console.error('Error saving revised wiki:', err);
      setError('Revised pages are shown but could not be saved to the server cache.');
    }
  }, [generatedPages, wikiStructure, effectiveRepoInfo, language, isComprehensiveView, selectedProviderState, selectedModelState, wikiMeta.selfReviewed]);
```

(b) Pass it to the modal: `onPagesRevised={handlePagesRevised}` on `<WikiReviewModal ...>`.

- [ ] **Step 5: Build (Docker) — must pass. Commit:**

```bash
git add src/components/WikiReviewModal.tsx "src/app/[owner]/[repo]/page.tsx"
git commit -m "feat: apply Model Review findings back onto wiki pages with user confirmation"
```

---

### Task 5: Full verification

- [ ] **Step 1:** `PYTHONPATH=/home/ubuntu/deepwiki-open .venv/bin/python -m pytest tests/unit -v -k "wiki_cache or anthropic" && PYTHONPATH=/home/ubuntu/deepwiki-open .venv/bin/python -m pytest test -v` — all pass.
- [ ] **Step 2:** Frontend build green (Docker; restore lockfiles).
- [ ] **Step 3:** Rebuild the staging image and recreate `deepwiki-staging` (ports 3001/8002, `PUBLIC_API_PORT=8002`, `~/.adalflow` mount — same `docker run` as previous deploys). Manual checks:
  1. Generate a wiki with self-review ON: log shows per-page "Reviewing {title}…" phases; backend log shows a second `/ws/chat` per page with `Using explicit rag_query for retrieval`; saved cache JSON contains `"self_reviewed": true`; "Generated by" line shows "· self-reviewed".
  2. Generate with the checkbox OFF: single pass per page, `self_reviewed: false`.
  3. Run a Model Review, click **Apply to wiki** → affected-pages confirmation appears → Apply → progress per page → summary; revised pages render immediately and persist across a hard reload (cache file mtime/`generated_at` updated).
  4. Apply button is disabled when viewing a review of a different model than the loaded wiki.
  5. Cancel at the confirmation step changes nothing.
