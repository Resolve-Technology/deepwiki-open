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

/**
 * Runs a single chat request to completion and resolves with the full text.
 * `onSocket` exposes the underlying WebSocket so callers can abort it (e.g.
 * when a modal closes mid-flight).
 */
export function runChatOnce(
  request: ChatCompletionRequest,
  timeoutMs: number = 600_000,
  onSocket?: (ws: WebSocket) => void,
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
    onSocket?.(ws);
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
  // Duplicate titles map to ALL pages sharing the title (better to revise an
  // extra page than to silently skip one).
  const byTitle = new Map<string, WikiPage[]>();
  for (const p of pages) {
    const key = p.title.trim().toLowerCase();
    const list = byTitle.get(key) ?? [];
    list.push(p);
    byTitle.set(key, list);
  }
  const seen = new Set<string>();
  const affected: WikiPage[] = [];
  for (const line of trimmed.split('\n')) {
    // Try the verbatim line first so titles that legitimately start with
    // numbering/dashes ("3. Architecture") still match; then the
    // bullet-stripped form for "- Title" style responses.
    const raw = line.trim().toLowerCase();
    const stripped = line.replace(/^[-*\d.\s]+/, '').trim().toLowerCase();
    const matches = byTitle.get(raw) ?? byTitle.get(stripped) ?? [];
    for (const page of matches) {
      if (!seen.has(page.id)) {
        seen.add(page.id);
        affected.push(page);
      }
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
  // Unwrap a whole-page ```markdown fence (models sometimes wrap the entire
  // reply). ONLY the explicit "markdown" language tag is treated as a wrapper:
  // a bare leading ``` could be the page's own first code block, and stripping
  // a lone trailing fence corrupts pages that end with a mermaid/code block —
  // the resulting unbalanced page would then be saved as a "correction".
  if (/^```markdown\s*\n/i.test(cleaned)) {
    cleaned = cleaned.replace(/^```markdown\s*\n/i, '').replace(/\n?```\s*$/, '').trim();
  }
  if (
    !cleaned ||
    cleaned.startsWith('Error:') ||
    new RegExp(`^${NO_CHANGES_TOKEN}\\b.{0,10}$`).test(cleaned)
  ) {
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
