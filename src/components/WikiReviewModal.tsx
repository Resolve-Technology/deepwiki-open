'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import Markdown from './Markdown';
import UserSelector from './UserSelector';
import RepoInfo from '@/types/repoinfo';
import getRepoUrl from '@/utils/getRepoUrl';
import { createChatWebSocket, closeWebSocket, ChatCompletionRequest } from '@/utils/websocketClient';
import { WikiPage } from '@/types/wiki/wikipage';
import { runChatOnce, buildPageRagQuery, buildAffectedPagesPrompt, parseAffectedPages, buildApplyReviewPrompt, parseRevisedContent } from '@/utils/wikiRevision';

// Char budget for wiki content embedded in the review prompt (~20k tokens),
// sized so smaller reviewer models (e.g. 32k-context vLLM) still fit the
// request. Pages are truncated proportionally AND the total is hard-capped.
const MAX_REVIEW_CHARS = 80_000;

interface WikiReview {
  repo: RepoInfo;
  language: string;
  reviewed_provider: string;
  reviewed_model: string;
  reviewer_provider: string;
  reviewer_model: string;
  content: string;
  created_at?: string;
}

interface WikiReviewModalProps {
  isOpen: boolean;
  onClose: () => void;
  repoInfo: RepoInfo;
  language: string;
  pages: WikiPage[];          // pages of the currently loaded wiki version
  reviewedProvider: string;   // provider/model that generated the loaded wiki
  reviewedModel: string;
  token?: string;
  /**
   * Receives pages whose content was revised by applying a review, together
   * with the wiki version they belong to (so the receiver can refuse the save
   * if the user switched versions mid-apply).
   */
  onPagesRevised?: (updated: Record<string, WikiPage>, target: { provider: string; model: string }) => void;
}

export default function WikiReviewModal({
  isOpen,
  onClose,
  repoInfo,
  language,
  pages,
  reviewedProvider,
  reviewedModel,
  token,
  onPagesRevised,
}: WikiReviewModalProps) {
  const [reviewerProvider, setReviewerProvider] = useState('');
  const [reviewerModel, setReviewerModel] = useState('');
  const [isCustomModel, setIsCustomModel] = useState(false);
  const [customModel, setCustomModel] = useState('');
  const [reviewContent, setReviewContent] = useState('');
  const [isReviewing, setIsReviewing] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [pastReviews, setPastReviews] = useState<WikiReview[]>([]);
  const webSocketRef = useRef<WebSocket | null>(null);
  const abortedRef = useRef(false);

  type ApplyPhase = 'idle' | 'classifying' | 'confirm' | 'revising' | 'done';
  const [applyPhase, setApplyPhase] = useState<ApplyPhase>('idle');
  const [applyTarget, setApplyTarget] = useState<WikiReview | null>(null);
  const [affectedPages, setAffectedPages] = useState<WikiPage[]>([]);
  const [applyProgress, setApplyProgress] = useState('');
  const [applySummary, setApplySummary] = useState('');
  const applyWsRef = useRef<WebSocket | null>(null);
  // Monotonic run token: bumping it invalidates any in-flight apply loop, and
  // each run only proceeds while the token still equals the value it captured
  // (a shared boolean could be reset by a NEW run, resurrecting an old loop).
  const applyRunRef = useRef(0);

  // Load past reviews whenever the modal opens
  useEffect(() => {
    if (!isOpen) return;
    const params = new URLSearchParams({
      owner: repoInfo.owner,
      repo: repoInfo.repo,
      repo_type: repoInfo.type,
      language: language,
    });
    fetch(`/api/wiki_review?${params.toString()}`)
      .then(res => (res.ok ? res.json() : []))
      .then((data: WikiReview[]) => setPastReviews(Array.isArray(data) ? data : []))
      .catch(() => setPastReviews([]));
  }, [isOpen, repoInfo.owner, repoInfo.repo, repoInfo.type, language]);

  // Close any in-flight websocket on unmount
  useEffect(() => () => closeWebSocket(webSocketRef.current), []);

  // Stop any in-flight review when the modal is closed (component stays mounted).
  useEffect(() => {
    if (!isOpen && webSocketRef.current) {
      abortedRef.current = true; // suppress the save in the onClose handler
      closeWebSocket(webSocketRef.current);
      webSocketRef.current = null;
      setIsReviewing(false);
    }
  }, [isOpen]);

  // Abort and reset any apply flow when the modal closes (component stays mounted).
  useEffect(() => {
    if (!isOpen) {
      applyRunRef.current++;
      closeWebSocket(applyWsRef.current);
      applyWsRef.current = null;
      setApplyPhase('idle');
      setApplyTarget(null);
      setAffectedPages([]);
      setApplyProgress('');
      setApplySummary('');
    }
  }, [isOpen]);

  // Short retrieval query so the backend can fetch code context via RAG even
  // though the full review prompt exceeds the websocket's large-input gate.
  const buildRagQuery = useCallback(() => {
    const titles = pages.map(p => p.title).join('; ').slice(0, 1000);
    const files = [...new Set(pages.flatMap(p => p.filePaths))].slice(0, 50).join(', ');
    return `Source code relevant to documentation covering: ${titles}. Key files: ${files}`.slice(0, 4000);
  }, [pages]);

  const buildReviewPrompt = useCallback(() => {
    const perPageBudget = Math.max(2000, Math.floor(MAX_REVIEW_CHARS / Math.max(pages.length, 1)));
    let wikiText = pages.map(page => {
      const body = page.content.length > perPageBudget
        ? page.content.slice(0, perPageBudget) + '\n\n...[truncated for review]'
        : page.content;
      return `<page title="${page.title}" files="${page.filePaths.join(', ')}">\n${body}\n</page>`;
    }).join('\n\n');
    if (wikiText.length > MAX_REVIEW_CHARS) {
      // Hard cap: per-page minimums can overshoot on wikis with many pages
      wikiText = wikiText.slice(0, MAX_REVIEW_CHARS) + '\n\n...[wiki truncated for review]';
    }
    return `You are reviewing AI-generated wiki documentation for the repository ${getRepoUrl(repoInfo)}.
The wiki below was generated by ${reviewedProvider}/${reviewedModel}. You have access to the repository's actual source code through the provided context — use it to verify claims.

Review the wiki for:
1. Factual accuracy against the actual code (wrong claims, invented APIs, incorrect architecture descriptions)
2. Completeness (important modules, flows or configuration that are missing or under-explained)
3. Clarity and structure
4. Mermaid diagram correctness

Be specific: name the page and quote the inaccurate statement when you flag something. End with a short summary verdict and an overall quality score from 1-10.

<wiki>
${wikiText}
</wiki>`;
  }, [pages, repoInfo, reviewedProvider, reviewedModel]);

  const saveReview = useCallback(async (content: string, provider: string, model: string) => {
    try {
      const response = await fetch('/api/wiki_review', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          // Never persist access tokens — strip before sending to the backend.
          repo: { ...repoInfo, token: null },
          language: language,
          reviewed_provider: reviewedProvider,
          reviewed_model: reviewedModel,
          reviewer_provider: provider,
          reviewer_model: model,
          content: content,
        }),
      });
      if (response.ok) {
        const saved = await response.json().catch(() => null);
        setPastReviews(prev => [{
          repo: repoInfo,
          language,
          reviewed_provider: reviewedProvider,
          reviewed_model: reviewedModel,
          reviewer_provider: provider,
          reviewer_model: model,
          content,
          created_at: saved?.created_at,
        }, ...prev.filter(r =>
          !(r.reviewed_provider === reviewedProvider && r.reviewed_model === reviewedModel &&
            r.reviewer_provider === provider && r.reviewer_model === model))]);
      } else {
        console.error('Failed to save wiki review:', response.status, await response.text());
      }
    } catch (err) {
      console.error('Error saving wiki review:', err);
    }
  }, [repoInfo, language, reviewedProvider, reviewedModel]);

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
    const runId = ++applyRunRef.current;
    try {
      const response = await runChatOnce(
        {
          ...wikiRequestBase(),
          messages: [{ role: 'user', content: buildAffectedPagesPrompt(review.content, pages) }],
        },
        600_000,
        ws => { applyWsRef.current = ws; },
      );
      if (applyRunRef.current !== runId) return; // modal closed or new run started mid-flight
      const affected = parseAffectedPages(response, pages);
      if (affected.length === 0) {
        setApplyPhase('done');
        setApplySummary('The review does not call for content changes to any page.');
        return;
      }
      setAffectedPages(affected);
      setApplyPhase('confirm');
    } catch (err) {
      if (applyRunRef.current !== runId) return;
      setApplyPhase('idle');
      setReviewError(`Could not determine affected pages: ${err instanceof Error ? err.message : err}`);
    }
  }, [pages, wikiRequestBase]);

  // Phase 2 (after user confirmation): revise each affected page.
  const confirmApply = useCallback(async () => {
    if (!applyTarget) return;
    // confirmApply runs as phase 2 of the run started by startApply, so it
    // captures the current token without bumping — closing the modal (which
    // bumps) or a new startApply (which bumps) both invalidate it.
    const runId = applyRunRef.current;
    setApplyPhase('revising');
    const updated: Record<string, WikiPage> = {};
    let revised = 0, unchanged = 0, failed = 0;
    for (let i = 0; i < affectedPages.length; i++) {
      if (applyRunRef.current !== runId) return; // modal closed mid-apply — discard
      const page = affectedPages[i];
      setApplyProgress(`Revising ${page.title} (${i + 1}/${affectedPages.length})...`);
      try {
        const response = await runChatOnce(
          {
            ...wikiRequestBase(),
            messages: [{ role: 'user', content: buildApplyReviewPrompt(page, applyTarget.content, getRepoUrl(repoInfo)) }],
            rag_query: buildPageRagQuery(page),
          },
          600_000,
          ws => { applyWsRef.current = ws; },
        );
        if (applyRunRef.current !== runId) return;
        const { content, changed } = parseRevisedContent(page.content, response);
        if (changed) {
          updated[page.id] = { ...page, content };
          revised++;
        } else {
          unchanged++;
        }
      } catch (err) {
        if (applyRunRef.current !== runId) return;
        console.warn(`Apply-review failed for ${page.title}, keeping original:`, err);
        failed++;
      }
    }
    if (applyRunRef.current !== runId) return;
    if (Object.keys(updated).length > 0) {
      onPagesRevised?.(updated, { provider: reviewedProvider, model: reviewedModel });
    }
    setApplyPhase('done');
    setApplyProgress('');
    setApplySummary(`Revised ${revised} page(s), ${unchanged} unchanged, ${failed} failed.`);
  }, [applyTarget, affectedPages, wikiRequestBase, repoInfo, onPagesRevised, reviewedProvider, reviewedModel]);

  const startReview = useCallback(() => {
    const effectiveModel = isCustomModel ? customModel : reviewerModel;
    if (!reviewerProvider || !effectiveModel) {
      setReviewError('Select a reviewer provider and model first.');
      return;
    }
    closeWebSocket(webSocketRef.current);
    setReviewError(null);
    setReviewContent('');
    setIsReviewing(true);
    abortedRef.current = false;

    const request: ChatCompletionRequest = {
      repo_url: getRepoUrl(repoInfo),
      type: repoInfo.type,
      messages: [{ role: 'user', content: buildReviewPrompt() }],
      provider: reviewerProvider,
      model: effectiveModel,
      language: language,
      token: token,
      rag_query: buildRagQuery(),
    };

    let content = '';
    webSocketRef.current = createChatWebSocket(
      request,
      (message: string) => {
        content += message;
        setReviewContent(content);
      },
      () => {
        setIsReviewing(false);
        setReviewError('Error during review generation. Check the reviewer model and try again.');
      },
      () => {
        setIsReviewing(false);
        if (abortedRef.current) {
          return; // user closed the modal mid-stream — discard, don't save
        }
        const finished = content.trim();
        if (finished && !finished.startsWith('Error:')) {
          saveReview(content, reviewerProvider, effectiveModel);
        } else if (finished) {
          // Backend reported a failure as message text — surface it, don't save it.
          setReviewError(finished.slice(0, 300));
        }
      },
    );
  }, [reviewerProvider, reviewerModel, isCustomModel, customModel, repoInfo, language, token, buildReviewPrompt, buildRagQuery, saveReview]);

  const canApply = (review: WikiReview) =>
    review.reviewed_provider === reviewedProvider && review.reviewed_model === reviewedModel &&
    pages.length > 0 && applyPhase === 'idle';

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto">
      <div className="flex min-h-screen items-center justify-center p-4 text-center bg-black/50">
        <div className="relative transform overflow-hidden rounded-lg bg-[var(--card-bg)] text-left shadow-xl transition-all sm:my-8 sm:max-w-3xl sm:w-full">
          {/* Header */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border-color)]">
            <h3 className="text-lg font-medium text-[var(--accent-primary)]">
              Model Review — wiki by {reviewedProvider}/{reviewedModel}
            </h3>
            <button
              type="button"
              onClick={onClose}
              className="text-[var(--muted)] hover:text-[var(--foreground)] focus:outline-none transition-colors"
            >
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* Body */}
          <div className="p-6 max-h-[70vh] overflow-y-auto">
            <p className="text-sm text-[var(--muted)] mb-4">
              Pick a different model to review this wiki against the repository&apos;s actual code.
            </p>
            <UserSelector
              provider={reviewerProvider}
              setProvider={setReviewerProvider}
              model={reviewerModel}
              setModel={setReviewerModel}
              isCustomModel={isCustomModel}
              setIsCustomModel={setIsCustomModel}
              customModel={customModel}
              setCustomModel={setCustomModel}
              showFileFilters={false}
            />

            {reviewError && (
              <div className="mt-4 p-3 rounded-md border border-[var(--highlight)]/30 text-sm text-[var(--highlight)]">
                {reviewError}
              </div>
            )}

            {reviewContent && (
              <div className="mt-4 p-4 rounded-md border border-[var(--border-color)] bg-[var(--background)]">
                <Markdown content={reviewContent} />
                {!isReviewing && (() => {
                  const effectiveReviewerModel = isCustomModel ? customModel : reviewerModel;
                  const liveReview: WikiReview = {
                    repo: repoInfo,
                    language,
                    reviewed_provider: reviewedProvider,
                    reviewed_model: reviewedModel,
                    reviewer_provider: reviewerProvider,
                    reviewer_model: effectiveReviewerModel,
                    content: reviewContent,
                  };
                  const providerModelMatch = liveReview.reviewed_provider === reviewedProvider && liveReview.reviewed_model === reviewedModel;
                  return (
                    <div className="mt-3">
                      <button
                        type="button"
                        onClick={() => startApply(liveReview)}
                        disabled={!canApply(liveReview)}
                        title={!providerModelMatch ? 'Load this review\'s wiki version first' : undefined}
                        className="px-3 py-1.5 text-sm rounded-md bg-[var(--accent-primary)]/90 text-white hover:bg-[var(--accent-primary)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                      >
                        Apply to wiki
                      </button>
                    </div>
                  );
                })()}
              </div>
            )}

            {/* Apply flow status blocks */}
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

            {/* Past reviews */}
            {pastReviews.length > 0 && !reviewContent && (
              <div className="mt-6">
                <h4 className="text-sm font-medium text-[var(--foreground)] mb-2">Saved reviews</h4>
                <div className="space-y-2">
                  {pastReviews.map((review) => {
                    const key = `${review.reviewed_provider}~${review.reviewed_model}~${review.reviewer_provider}~${review.reviewer_model}`;
                    const providerModelMatch = review.reviewed_provider === reviewedProvider && review.reviewed_model === reviewedModel;
                    return (
                      <div
                        key={key}
                        className="flex items-center gap-2 p-3 rounded-md border border-[var(--border-color)] hover:bg-[var(--background)] transition-colors"
                      >
                        <button
                          type="button"
                          onClick={() => setReviewContent(review.content)}
                          className="flex-1 text-left"
                        >
                          <span className="text-sm text-[var(--foreground)]">
                            {review.reviewed_provider}/{review.reviewed_model} reviewed by {review.reviewer_provider}/{review.reviewer_model}
                          </span>
                          {review.created_at && (
                            <span className="block text-xs text-[var(--muted)] mt-1">
                              {new Date(review.created_at).toLocaleString()}
                            </span>
                          )}
                        </button>
                        <button
                          type="button"
                          onClick={() => startApply(review)}
                          disabled={!canApply(review)}
                          title={!providerModelMatch ? 'Load this review\'s wiki version first' : undefined}
                          className="flex-shrink-0 px-2 py-1 text-xs rounded-md bg-[var(--accent-primary)]/90 text-white hover:bg-[var(--accent-primary)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                        >
                          Apply
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-[var(--border-color)]">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium rounded-md border border-[var(--border-color)]/50 text-[var(--muted)] bg-transparent hover:bg-[var(--background)] hover:text-[var(--foreground)] transition-colors"
            >
              Close
            </button>
            <button
              type="button"
              onClick={startReview}
              disabled={isReviewing || pages.length === 0 || applyPhase === 'classifying' || applyPhase === 'revising'}
              className="px-4 py-2 text-sm font-medium rounded-md border border-transparent bg-[var(--accent-primary)]/90 text-white hover:bg-[var(--accent-primary)] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {isReviewing ? 'Reviewing…' : 'Start Review'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
