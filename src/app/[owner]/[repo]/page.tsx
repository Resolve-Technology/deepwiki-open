/* eslint-disable @typescript-eslint/no-unused-vars */
'use client';

import Ask from '@/components/Ask';
import Markdown from '@/components/Markdown';
import ModelSelectionModal from '@/components/ModelSelectionModal';
import WikiReviewModal from '@/components/WikiReviewModal';
import ThemeToggle from '@/components/theme-toggle';
import WikiTreeView from '@/components/WikiTreeView';
import { useLanguage } from '@/contexts/LanguageContext';
import { RepoInfo } from '@/types/repoinfo';
import getRepoUrl from '@/utils/getRepoUrl';
import Link from 'next/link';
import { useParams, useSearchParams } from 'next/navigation';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { FaBitbucket, FaBookOpen, FaComments, FaDownload, FaExclamationTriangle, FaFileExport, FaFolder, FaGithub, FaGitlab, FaHome, FaSync, FaTimes } from 'react-icons/fa';
// Define the WikiSection and WikiStructure types directly in this file
// since the imported types don't have the sections and rootSections properties
interface WikiSection {
  id: string;
  title: string;
  pages: string[];
  subsections?: string[];
}

interface WikiPage {
  id: string;
  title: string;
  content: string;
  filePaths: string[];
  importance: 'high' | 'medium' | 'low';
  relatedPages: string[];
  citations?: Record<string, {
    status: 'verified' | 'broken';
    filePath: string;
    startLine?: number;
    endLine?: number;
    snippet?: string;
    reason?: string;
  }>;
  parentId?: string;
  isSection?: boolean;
  children?: string[];
}

interface WikiStructure {
  id: string;
  title: string;
  description: string;
  pages: WikiPage[];
  sections: WikiSection[];
  rootSections: string[];
}

// Server-side generation job, as returned by /api/wiki_jobs
interface WikiJobStatus {
  id: string;
  repo: { owner: string; repo: string; type: string; repoUrl?: string | null; localPath?: string | null };
  language: string;
  provider: string;
  model: string;
  comprehensive: boolean;
  self_review: boolean;
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'interrupted';
  progress: { phase: string; pages_total: number; pages_done: number; current_page_title: string };
  stats?: Record<string, unknown>;
  error?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

const JOB_ACTIVE_STATUSES = ['queued', 'running'];

// Add CSS styles for wiki with Japanese aesthetic
const wikiStyles = `
  .prose code {
    @apply bg-[var(--background)]/70 px-1.5 py-0.5 rounded font-mono text-xs border border-[var(--border-color)];
  }

  .prose pre {
    @apply bg-[var(--background)]/80 text-[var(--foreground)] rounded-md p-4 overflow-x-auto border border-[var(--border-color)] shadow-sm;
  }

  .prose h1, .prose h2, .prose h3, .prose h4 {
    @apply font-serif text-[var(--foreground)];
  }

  .prose p {
    @apply text-[var(--foreground)] leading-relaxed;
  }

  .prose a {
    @apply text-[var(--accent-primary)] hover:text-[var(--highlight)] transition-colors no-underline border-b border-[var(--border-color)] hover:border-[var(--accent-primary)];
  }

  .prose blockquote {
    @apply border-l-4 border-[var(--accent-primary)]/30 bg-[var(--background)]/30 pl-4 py-1 italic;
  }

  .prose ul, .prose ol {
    @apply text-[var(--foreground)];
  }

  .prose table {
    @apply border-collapse border border-[var(--border-color)];
  }

  .prose th {
    @apply bg-[var(--background)]/70 text-[var(--foreground)] p-2 border border-[var(--border-color)];
  }

  .prose td {
    @apply p-2 border border-[var(--border-color)];
  }
`;

// Helper function to generate cache key for localStorage
const getCacheKey = (owner: string, repo: string, repoType: string, language: string, isComprehensive: boolean = true): string => {
  return `deepwiki_cache_${repoType}_${owner}_${repo}_${language}_${isComprehensive ? 'comprehensive' : 'concise'}`;
};

export default function RepoWikiPage() {
  // Get route parameters and search params
  const params = useParams();
  const searchParams = useSearchParams();

  // Extract owner and repo from route params
  const owner = params.owner as string;
  const repo = params.repo as string;

  // Extract tokens from search params
  const token = searchParams.get('token') || '';
  const localPath = searchParams.get('local_path') ? decodeURIComponent(searchParams.get('local_path') || '') : undefined;
  const repoUrl = searchParams.get('repo_url') ? decodeURIComponent(searchParams.get('repo_url') || '') : undefined;
  const providerParam = searchParams.get('provider') || '';
  const modelParam = searchParams.get('model') || '';
  const isCustomModelParam = searchParams.get('is_custom_model') === 'true';
  const customModelParam = searchParams.get('custom_model') || '';
  const language = searchParams.get('language') || 'en';
  const repoHost = (() => {
    if (!repoUrl) return '';
    try {
      return new URL(repoUrl).hostname.toLowerCase();
    } catch (e) {
      console.warn(`Invalid repoUrl provided: ${repoUrl}`);
      return '';
    }
  })();
  const repoType = repoHost?.includes('bitbucket')
    ? 'bitbucket'
    : repoHost?.includes('gitlab')
      ? 'gitlab'
      : repoHost?.includes('github')
        ? 'github'
        : searchParams.get('type') || 'github';

  // Import language context for translations
  const { messages } = useLanguage();

  // Initialize repo info
  const repoInfo = useMemo<RepoInfo>(() => ({
    owner,
    repo,
    type: repoType,
    token: token || null,
    localPath: localPath || null,
    repoUrl: repoUrl || null
  }), [owner, repo, repoType, localPath, repoUrl, token]);

  // State variables
  const [isLoading, setIsLoading] = useState(true);
  const [loadingMessage, setLoadingMessage] = useState<string | undefined>(
    messages.loading?.initializing || 'Initializing wiki generation...'
  );
  const [error, setError] = useState<string | null>(null);
  const [wikiStructure, setWikiStructure] = useState<WikiStructure | undefined>();
  const [currentPageId, setCurrentPageId] = useState<string | undefined>();
  const [generatedPages, setGeneratedPages] = useState<Record<string, WikiPage>>({});
  const [isExporting, setIsExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [currentToken, setCurrentToken] = useState(token); // Track current effective token
  const [effectiveRepoInfo, setEffectiveRepoInfo] = useState(repoInfo); // Track effective repo info with cached data
  const [embeddingError, setEmbeddingError] = useState(false);

  // Server-side generation job this page is watching (if any)
  const [activeJob, setActiveJob] = useState<WikiJobStatus | null>(null);
  // Cache miss with no running job: show the "Generate this wiki" panel
  // instead of auto-generating (a bare page visit must never spend tokens).
  const [cacheMiss, setCacheMiss] = useState(false);
  const lastPagesDoneRef = useRef(0);

  // Model selection state variables
  const [selectedProviderState, setSelectedProviderState] = useState(providerParam);
  const [selectedModelState, setSelectedModelState] = useState(modelParam);
  const [isCustomSelectedModelState, setIsCustomSelectedModelState] = useState(isCustomModelParam);
  const [customSelectedModelState, setCustomSelectedModelState] = useState(customModelParam);
  const [showModelOptions, setShowModelOptions] = useState(false); // Controls whether to show model options
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [wikiMeta, setWikiMeta] = useState<{ generatedAt?: string; repoCommit?: string; selfReviewed?: boolean }>({});
  const excludedDirs = searchParams.get('excluded_dirs') || '';
  const excludedFiles = searchParams.get('excluded_files') || '';
  const [modelExcludedDirs, setModelExcludedDirs] = useState(excludedDirs);
  const [modelExcludedFiles, setModelExcludedFiles] = useState(excludedFiles);
  const includedDirs = searchParams.get('included_dirs') || '';
  const includedFiles = searchParams.get('included_files') || '';
  const [modelIncludedDirs, setModelIncludedDirs] = useState(includedDirs);
  const [modelIncludedFiles, setModelIncludedFiles] = useState(includedFiles);


  // Wiki type state - default to comprehensive view
  const isComprehensiveParam = searchParams.get('comprehensive') !== 'false';
  const isSelfReviewParam = searchParams.get('self_review') !== 'false';
  const [isComprehensiveView, setIsComprehensiveView] = useState(isComprehensiveParam);
  const [isSelfReviewEnabled, setIsSelfReviewEnabled] = useState(isSelfReviewParam);

  // Create a flag to ensure the effect only runs once
  const effectRan = React.useRef(false);

  // State for Ask modal
  const [isAskModalOpen, setIsAskModalOpen] = useState(false);
  const askComponentRef = useRef<{ clearConversation: () => void } | null>(null);

  // Authentication state
  const [authRequired, setAuthRequired] = useState<boolean>(false);
  const [authCode, setAuthCode] = useState<string>('');
  const [isAuthLoading, setIsAuthLoading] = useState<boolean>(true);

  // Add useEffect to handle scroll reset
  useEffect(() => {
    // Scroll to top when currentPageId changes
    const wikiContent = document.getElementById('wiki-content');
    if (wikiContent) {
      wikiContent.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }, [currentPageId]);

  // close the modal when escape is pressed
  useEffect(() => {
    const handleEsc = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setIsAskModalOpen(false);
      }
    };

    if (isAskModalOpen) {
      window.addEventListener('keydown', handleEsc);
    }

    // Cleanup on unmount or when modal closes
    return () => {
      window.removeEventListener('keydown', handleEsc);
    };
  }, [isAskModalOpen]);

  // Fetch authentication status on component mount
  useEffect(() => {
    const fetchAuthStatus = async () => {
      try {
        setIsAuthLoading(true);
        const response = await fetch('/api/auth/status');
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();
        setAuthRequired(data.auth_required);
      } catch (err) {
        console.error("Failed to fetch auth status:", err);
        // Assuming auth is required if fetch fails to avoid blocking UI for safety
        setAuthRequired(true);
      } finally {
        setIsAuthLoading(false);
      }
    };

    fetchAuthStatus();
  }, []);

  // --- Server-side generation: cache reads + job enqueue/polling -----------
  // The old in-browser orchestration (structure call, per-page websocket
  // generation, self-review) moved into the backend job queue; this page is
  // now a viewer that enqueues jobs and polls their progress.

  // Read the selected version's wiki from the server cache; returns whether a
  // usable wiki was loaded. Never flips global loading state — callers decide.
  const refreshFromCache = useCallback(async (override?: { provider?: string; model?: string }): Promise<boolean> => {
    try {
      const params = new URLSearchParams({
        owner: effectiveRepoInfo.owner,
        repo: effectiveRepoInfo.repo,
        repo_type: effectiveRepoInfo.type,
        language: language,
        comprehensive: isComprehensiveView.toString(),
      });
      // When a specific provider/model is selected (from URL params or the model
      // selector), load that exact cached version; otherwise the backend returns
      // the most recently generated one.
      const provider = override?.provider ?? selectedProviderState;
      const model = override?.model ?? selectedModelState;
      if (provider && model) {
        params.append('provider', provider);
        params.append('model', model);
      }
      const response = await fetch(`/api/wiki_cache?${params.toString()}`);

      if (!response.ok) {
        console.error('Error fetching wiki cache from server:', response.status, await response.text());
        return false;
      }

      const cachedData = await response.json(); // Returns null if no cache
      if (!(cachedData && cachedData.wiki_structure && cachedData.generated_pages && Object.keys(cachedData.generated_pages).length > 0)) {
        console.log('No valid wiki data in server cache or cache is empty.');
        return false;
      }

      console.log('Using server-cached wiki data');
      if (cachedData.model) {
        setSelectedModelState(cachedData.model);
      }
      if (cachedData.provider) {
        setSelectedProviderState(cachedData.provider);
      }
      setWikiMeta({ generatedAt: cachedData.generated_at, repoCommit: cachedData.repo_commit, selfReviewed: cachedData.self_reviewed });

      // Update repoInfo
      if (cachedData.repo) {
        setEffectiveRepoInfo(cachedData.repo);
      } else if (cachedData.repo_url && !effectiveRepoInfo.repoUrl) {
        const updatedRepoInfo = { ...effectiveRepoInfo, repoUrl: cachedData.repo_url };
        setEffectiveRepoInfo(updatedRepoInfo); // Update effective repo info state
        console.log('Using cached repo_url:', cachedData.repo_url);
      }

      // Ensure the cached structure has sections and rootSections
      const cachedStructure = {
        ...cachedData.wiki_structure,
        sections: cachedData.wiki_structure.sections || [],
        rootSections: cachedData.wiki_structure.rootSections || []
      };

      // If sections or rootSections are missing, create intelligent ones based on page titles
      if (!cachedStructure.sections.length || !cachedStructure.rootSections.length) {
        const pages = cachedStructure.pages;
        const sections: WikiSection[] = [];
        const rootSections: string[] = [];

        // Group pages by common prefixes or categories
        const pageClusters = new Map<string, WikiPage[]>();

        // Define common categories that might appear in page titles
        const categories = [
          { id: 'overview', title: 'Overview', keywords: ['overview', 'introduction', 'about'] },
          { id: 'architecture', title: 'Architecture', keywords: ['architecture', 'structure', 'design', 'system'] },
          { id: 'features', title: 'Core Features', keywords: ['feature', 'functionality', 'core'] },
          { id: 'components', title: 'Components', keywords: ['component', 'module', 'widget'] },
          { id: 'api', title: 'API', keywords: ['api', 'endpoint', 'service', 'server'] },
          { id: 'data', title: 'Data Flow', keywords: ['data', 'flow', 'pipeline', 'storage'] },
          { id: 'models', title: 'Models', keywords: ['model', 'ai', 'ml', 'integration'] },
          { id: 'ui', title: 'User Interface', keywords: ['ui', 'interface', 'frontend', 'page'] },
          { id: 'setup', title: 'Setup & Configuration', keywords: ['setup', 'config', 'installation', 'deploy'] }
        ];

        // Initialize clusters with empty arrays
        categories.forEach(category => {
          pageClusters.set(category.id, []);
        });

        // Add an "Other" category for pages that don't match any category
        pageClusters.set('other', []);

        // Assign pages to categories based on title keywords
        pages.forEach((page: WikiPage) => {
          const title = page.title.toLowerCase();
          let assigned = false;

          // Try to find a matching category
          for (const category of categories) {
            if (category.keywords.some(keyword => title.includes(keyword))) {
              pageClusters.get(category.id)?.push(page);
              assigned = true;
              break;
            }
          }

          // If no category matched, put in "Other"
          if (!assigned) {
            pageClusters.get('other')?.push(page);
          }
        });

        // Create sections for non-empty categories
        for (const [categoryId, categoryPages] of pageClusters.entries()) {
          if (categoryPages.length > 0) {
            const category = categories.find(c => c.id === categoryId) ||
                            { id: categoryId, title: categoryId === 'other' ? 'Other' : categoryId.charAt(0).toUpperCase() + categoryId.slice(1) };

            const sectionId = `section-${categoryId}`;
            sections.push({
              id: sectionId,
              title: category.title,
              pages: categoryPages.map((p: WikiPage) => p.id)
            });
            rootSections.push(sectionId);

            // Update page parentId
            categoryPages.forEach((page: WikiPage) => {
              page.parentId = sectionId;
            });
          }
        }

        // If we still have no sections (unlikely), fall back to importance-based grouping
        if (sections.length === 0) {
          const highImportancePages = pages.filter((p: WikiPage) => p.importance === 'high').map((p: WikiPage) => p.id);
          const mediumImportancePages = pages.filter((p: WikiPage) => p.importance === 'medium').map((p: WikiPage) => p.id);
          const lowImportancePages = pages.filter((p: WikiPage) => p.importance === 'low').map((p: WikiPage) => p.id);

          if (highImportancePages.length > 0) {
            sections.push({
              id: 'section-high',
              title: 'Core Components',
              pages: highImportancePages
            });
            rootSections.push('section-high');
          }

          if (mediumImportancePages.length > 0) {
            sections.push({
              id: 'section-medium',
              title: 'Key Features',
              pages: mediumImportancePages
            });
            rootSections.push('section-medium');
          }

          if (lowImportancePages.length > 0) {
            sections.push({
              id: 'section-low',
              title: 'Additional Information',
              pages: lowImportancePages
            });
            rootSections.push('section-low');
          }
        }

        cachedStructure.sections = sections;
        cachedStructure.rootSections = rootSections;
      }

      setWikiStructure(cachedStructure);
      setGeneratedPages(cachedData.generated_pages);
      // Keep the user's page selection across incremental refreshes
      setCurrentPageId(prev =>
        prev && cachedStructure.pages.some((p: WikiPage) => p.id === prev)
          ? prev
          : (cachedStructure.pages.length > 0 ? cachedStructure.pages[0].id : undefined));
      setEmbeddingError(false);
      setCacheMiss(false);
      return true;
    } catch (error) {
      console.error('Error loading from server cache:', error);
      return false;
    }
  }, [effectiveRepoInfo, language, isComprehensiveView, selectedProviderState, selectedModelState]);

  // Does a job belong to the wiki version this page is showing?
  const jobMatchesThisWiki = useCallback((job: WikiJobStatus): boolean =>
    job.repo.owner === effectiveRepoInfo.owner &&
    job.repo.repo === effectiveRepoInfo.repo &&
    job.repo.type === effectiveRepoInfo.type &&
    job.language === language &&
    (!selectedProviderState || job.provider === selectedProviderState) &&
    (!selectedModelState || job.model === selectedModelState),
  [effectiveRepoInfo, language, selectedProviderState, selectedModelState]);

  // Find a queued/running job for this wiki (used on mount and after a 409)
  const findActiveJob = useCallback(async (): Promise<WikiJobStatus | null> => {
    try {
      const response = await fetch('/api/wiki_jobs');
      if (!response.ok) return null;
      const data = await response.json();
      const jobs: WikiJobStatus[] = data.jobs || [];
      return jobs.find(j => JOB_ACTIVE_STATUSES.includes(j.status) && jobMatchesThisWiki(j)) || null;
    } catch (err) {
      console.error('Error listing wiki jobs:', err);
      return null;
    }
  }, [jobMatchesThisWiki]);

  // Enqueue a server-side generation job; on 409 attach to the existing one
  const enqueueJob = useCallback(async (forceRegenerate: boolean, overrideProvider?: string, overrideModel?: string, overrideToken?: string): Promise<WikiJobStatus | null> => {
    const body = {
      repo: { ...effectiveRepoInfo, token: overrideToken || currentToken || null },
      language: language,
      provider: overrideProvider ?? selectedProviderState,
      model: overrideModel ?? selectedModelState,
      comprehensive: isComprehensiveView,
      self_review: isSelfReviewEnabled,
      force_regenerate: forceRegenerate,
      excluded_dirs: modelExcludedDirs || undefined,
      excluded_files: modelExcludedFiles || undefined,
      included_dirs: modelIncludedDirs || undefined,
      included_files: modelIncludedFiles || undefined,
      authorization_code: authCode || undefined,
    };
    const response = await fetch('/api/wiki_jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (response.status === 409) {
      // Already queued/running for this version — just attach to it
      console.log('Generation already in progress, attaching to the running job');
      return await findActiveJob();
    }
    if (response.status === 401) {
      throw new Error('Failed to validate the authorization code');
    }
    if (!response.ok) {
      const errorText = await response.text().catch(() => 'No error details available');
      throw new Error(`Failed to start generation (${response.status}): ${errorText}`);
    }
    const { job_id } = await response.json();
    const jobResponse = await fetch(`/api/wiki_jobs/${job_id}`);
    if (jobResponse.ok) {
      return await jobResponse.json();
    }
    return await findActiveJob();
  }, [effectiveRepoInfo, currentToken, language, selectedProviderState, selectedModelState, isComprehensiveView, isSelfReviewEnabled, modelExcludedDirs, modelExcludedFiles, modelIncludedDirs, modelIncludedFiles, authCode, findActiveJob]);

  // Dismiss a finished job: removed server-side so it leaves the home page
  // JobsPanel (and the journal) too, not just this view.
  const dismissActiveJob = useCallback(async () => {
    if (!activeJob) return;
    const jobId = activeJob.id;
    setActiveJob(null);
    try {
      const params = authCode ? `?authorization_code=${encodeURIComponent(authCode)}` : '';
      await fetch(`/api/wiki_jobs/${jobId}${params}`, { method: 'DELETE' });
    } catch (err) {
      console.error('Error removing job:', err); // view already cleared; best-effort
    }
  }, [activeJob, authCode]);

  // Cancel the watched job (auth code passed like the refresh flow does)
  const cancelActiveJob = useCallback(async () => {
    if (!activeJob) return;
    try {
      const params = authCode ? `?authorization_code=${encodeURIComponent(authCode)}` : '';
      const response = await fetch(`/api/wiki_jobs/${activeJob.id}/cancel${params}`, { method: 'POST' });
      if (!response.ok) {
        if (response.status === 401) {
          setError('Failed to validate the authorization code');
          return;
        }
        console.error('Error cancelling job:', response.status, await response.text());
      }
    } catch (err) {
      console.error('Error cancelling job:', err);
    }
  }, [activeJob, authCode]);

  // Poll the watched job every 3s; reload the cache as pages land
  useEffect(() => {
    if (!activeJob || !JOB_ACTIVE_STATUSES.includes(activeJob.status)) return;
    let stopped = false;

    const poll = async () => {
      try {
        const response = await fetch(`/api/wiki_jobs/${activeJob.id}`);
        if (!response.ok || stopped) return;
        const job: WikiJobStatus = await response.json();
        if (stopped) return;
        setActiveJob(job);
        // New pages were saved incrementally — pull them in
        if (job.progress.pages_done > lastPagesDoneRef.current) {
          lastPagesDoneRef.current = job.progress.pages_done;
          const loaded = await refreshFromCache({ provider: job.provider, model: job.model });
          if (loaded && !stopped) {
            setIsLoading(false);
            setLoadingMessage(undefined);
          }
        }
        if (job.status === 'done') {
          await refreshFromCache({ provider: job.provider, model: job.model });
          if (stopped) return;
          setIsLoading(false);
          setLoadingMessage(undefined);
          setActiveJob(null);
        } else if (job.status === 'failed' || job.status === 'cancelled' || job.status === 'interrupted') {
          // Pages generated so far are in the cache — show what exists
          await refreshFromCache({ provider: job.provider, model: job.model });
          if (stopped) return;
          setIsLoading(false);
          setLoadingMessage(undefined);
        }
      } catch (err) {
        console.error('Error polling wiki job:', err);
      }
    };

    poll();
    const interval = setInterval(poll, 3000);
    return () => {
      stopped = true;
      clearInterval(interval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeJob?.id, activeJob?.status, refreshFromCache]);

  // Function to export wiki content
  const exportWiki = useCallback(async (format: 'markdown' | 'json') => {
    if (!wikiStructure || Object.keys(generatedPages).length === 0) {
      setExportError('No wiki content to export');
      return;
    }

    try {
      setIsExporting(true);
      setExportError(null);
      setLoadingMessage(`${language === 'ja' ? 'Wikiを' : 'Exporting wiki as '} ${format} ${language === 'ja' ? 'としてエクスポート中...' : '...'}`);

      // Prepare the pages for export
      const pagesToExport = wikiStructure.pages.map(page => {
        // Use the generated content if available, otherwise use an empty string
        const content = generatedPages[page.id]?.content || 'Content not generated';
        return {
          ...page,
          content
        };
      });

      // Get repository URL
      const repoUrl = getRepoUrl(effectiveRepoInfo);

      // Make API call to export wiki
      const response = await fetch(`/export/wiki`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          repo_url: repoUrl,
          type: effectiveRepoInfo.type,
          pages: pagesToExport,
          format,
          provider: selectedProviderState,
          model: selectedModelState,
          generated_at: wikiMeta.generatedAt,
          repo_commit: wikiMeta.repoCommit
        })
      });

      if (!response.ok) {
        const errorText = await response.text().catch(() => 'No error details available');
        throw new Error(`Error exporting wiki: ${response.status} - ${errorText}`);
      }

      // Get the filename from the Content-Disposition header if available
      const contentDisposition = response.headers.get('Content-Disposition');
      let filename = `${effectiveRepoInfo.repo}_wiki.${format === 'markdown' ? 'md' : 'json'}`;

      if (contentDisposition) {
        const filenameMatch = contentDisposition.match(/filename=(.+)/);
        if (filenameMatch && filenameMatch[1]) {
          filename = filenameMatch[1].replace(/"/g, '');
        }
      }

      // Convert the response to a blob and download it
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);

    } catch (err) {
      console.error('Error exporting wiki:', err);
      const errorMessage = err instanceof Error ? err.message : 'Unknown error during export';
      setExportError(errorMessage);
    } finally {
      setIsExporting(false);
      setLoadingMessage(undefined);
    }
  }, [wikiStructure, generatedPages, effectiveRepoInfo, language, selectedProviderState, selectedModelState, wikiMeta]);

  // Persist pages revised by applying a Model Review: merge into state and
  // save explicitly (the auto-save effect is gated off for cache-loaded wikis).
  const handlePagesRevised = useCallback(async (updated: Record<string, WikiPage>, target: { provider: string; model: string }) => {
    // The apply runs for minutes; refuse if the user switched wiki versions
    // meanwhile — merging model A's revisions into model B's pages would
    // silently corrupt the other version's cache.
    if (target.provider !== selectedProviderState || target.model !== selectedModelState) {
      setError(`Review was applied to ${target.provider}/${target.model}, but ${selectedProviderState}/${selectedModelState} is now loaded — revisions were discarded. Reload that version and apply again.`);
      return;
    }
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

  // Submit / Regenerate from the model-selection modal. Submit switches to the
  // selected version (loads its cache; enqueues a generation on a miss);
  // Regenerate always enqueues with force_regenerate so the server deletes the
  // target version and rebuilds it. Generation itself runs server-side.
  const confirmRefresh = useCallback(async (newToken?: string, forceRegenerate: boolean = false, overrideProvider?: string, overrideModel?: string) => {
    setShowModelOptions(false);

    if (authRequired && !authCode) {
      console.error("Authorization code is required");
      setError('Authorization code is required');
      return;
    }

    // Update token if provided
    if (newToken) {
      // Update current token state
      setCurrentToken(newToken);
      // Update the URL parameters to include the new token
      const currentUrl = new URL(window.location.href);
      currentUrl.searchParams.set('token', newToken);
      window.history.replaceState({}, '', currentUrl.toString());
    }

    const provider = overrideProvider ?? selectedProviderState;
    const model = overrideModel ?? selectedModelState;
    if (overrideProvider) setSelectedProviderState(overrideProvider);
    if (overrideModel) setSelectedModelState(overrideModel);

    // Clear the localStorage cache (if any remnants from older versions)
    const localStorageCacheKey = getCacheKey(effectiveRepoInfo.owner, effectiveRepoInfo.repo, effectiveRepoInfo.type, language, isComprehensiveView);
    localStorage.removeItem(localStorageCacheKey);

    setError(null);
    setExportError(null);
    setEmbeddingError(false);
    setCacheMiss(false);

    try {
      if (!forceRegenerate) {
        // Submit: the selected version may already be cached — just load it
        setIsLoading(true);
        setLoadingMessage(messages.loading?.fetchingCache || 'Checking for cached wiki...');
        const loaded = await refreshFromCache({ provider, model });
        setIsLoading(false);
        setLoadingMessage(undefined);
        if (loaded) {
          // Still re-attach to a running job for this version, if any
          const job = await findActiveJob();
          if (job) {
            lastPagesDoneRef.current = job.progress?.pages_done ?? 0;
            setActiveJob(job);
          }
          return;
        }
      } else {
        // Regenerate: drop the stale view; pages reappear as the job saves them
        setWikiStructure(undefined);
        setGeneratedPages({});
        setCurrentPageId(undefined);
        setIsLoading(false);
        setLoadingMessage(undefined);
      }

      const job = await enqueueJob(forceRegenerate, provider, model, newToken);
      if (job) {
        lastPagesDoneRef.current = forceRegenerate ? 0 : (job.progress?.pages_done ?? 0);
        setActiveJob(job);
        setIsLoading(false);
        setLoadingMessage(undefined);
      } else {
        setError('Failed to start generation: job was not created');
      }
    } catch (err) {
      console.error('Error starting generation:', err);
      setIsLoading(false);
      setLoadingMessage(undefined);
      setError(err instanceof Error ? err.message : 'Failed to start generation');
    }
  }, [effectiveRepoInfo, language, messages.loading, selectedProviderState, selectedModelState, isComprehensiveView, authCode, authRequired, refreshFromCache, enqueueJob, findActiveJob]);

  // Load the wiki when the page mounts: cache first, then attach to any
  // running job. A cache miss with no job shows the "Generate this wiki"
  // panel — a bare page visit never auto-generates (no token spend).
  useEffect(() => {
    if (effectRan.current === false) {
      effectRan.current = true; // Set to true immediately to prevent re-entry due to StrictMode

      const loadData = async () => {
        setIsLoading(true);
        setCacheMiss(false);
        setLoadingMessage(messages.loading?.fetchingCache || 'Checking for cached wiki...');

        const loaded = await refreshFromCache();

        // Re-attach to a generation that is already running for this version
        const job = await findActiveJob();
        if (job) {
          lastPagesDoneRef.current = job.progress?.pages_done ?? 0;
          setActiveJob(job);
          setIsLoading(false);
          setLoadingMessage(undefined);
          return;
        }

        if (loaded) {
          setIsLoading(false);
          setLoadingMessage(undefined);
          return;
        }

        // No cache, no job: offer generation instead of starting one
        setCacheMiss(true);
        setIsLoading(false);
        setLoadingMessage(undefined);
      };

      loadData();

    } else {
      console.log('Skipping duplicate repository fetch/cache check');
    }
  }, [effectiveRepoInfo, effectiveRepoInfo.owner, effectiveRepoInfo.repo, effectiveRepoInfo.type, language, refreshFromCache, findActiveJob, messages.loading?.fetchingCache, isComprehensiveView, selectedProviderState, selectedModelState, refreshNonce]);

  const handlePageSelect = (pageId: string) => {
    if (currentPageId != pageId) {
      setCurrentPageId(pageId)
    }
  };

  const [isModelSelectionModalOpen, setIsModelSelectionModalOpen] = useState(false);
  const [isReviewModalOpen, setIsReviewModalOpen] = useState(false);

  // Re-enqueue after a failed/cancelled/interrupted job (fresh full run)
  const retryJob = useCallback(async () => {
    if (!activeJob) return;
    const { provider, model, id: oldJobId } = activeJob;
    setActiveJob(null);
    setError(null);
    // Best-effort: drop the failed entry the new run replaces
    try {
      const params = authCode ? `?authorization_code=${encodeURIComponent(authCode)}` : '';
      await fetch(`/api/wiki_jobs/${oldJobId}${params}`, { method: 'DELETE' });
    } catch { /* ignore */ }
    try {
      const job = await enqueueJob(false, provider, model);
      if (job) {
        lastPagesDoneRef.current = job.progress?.pages_done ?? 0;
        setActiveJob(job);
      }
    } catch (err) {
      console.error('Error retrying generation:', err);
      setError(err instanceof Error ? err.message : 'Failed to retry generation');
    }
  }, [activeJob, enqueueJob, authCode]);

  // Enqueue for the current selection (the cache-miss "Generate" button)
  const startGeneration = useCallback(async () => {
    setError(null);
    setCacheMiss(false);
    try {
      const job = await enqueueJob(false);
      if (job) {
        lastPagesDoneRef.current = job.progress?.pages_done ?? 0;
        setActiveJob(job);
      }
    } catch (err) {
      console.error('Error starting generation:', err);
      setCacheMiss(true);
      setError(err instanceof Error ? err.message : 'Failed to start generation');
    }
  }, [enqueueJob]);

  const jobElapsedSeconds = activeJob?.started_at
    ? Math.max(0, Math.round((Date.now() - new Date(activeJob.started_at).getTime()) / 1000))
    : 0;

  return (
    <div className="h-screen paper-texture p-4 md:p-8 flex flex-col">
      <style>{wikiStyles}</style>

      <header className="max-w-[90%] xl:max-w-[1400px] mx-auto mb-8 h-fit w-full">
        <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
          <div className="flex items-center gap-4">
            <Link href="/" className="text-[var(--accent-primary)] hover:text-[var(--highlight)] flex items-center gap-1.5 transition-colors border-b border-[var(--border-color)] hover:border-[var(--accent-primary)] pb-0.5">
              <FaHome /> {messages.repoPage?.home || 'Home'}
            </Link>
          </div>
        </div>
      </header>

      <main className="flex-1 max-w-[90%] xl:max-w-[1400px] mx-auto overflow-y-auto">
        {/* Server-side generation progress panel */}
        {!isLoading && activeJob && (
          <div className="p-4 mb-4 bg-[var(--card-bg)] rounded-lg shadow-custom card-japanese">
            {JOB_ACTIVE_STATUSES.includes(activeJob.status) ? (
              <div>
                <div className="flex items-center justify-between gap-4 mb-2">
                  <p className="text-sm text-[var(--foreground)] font-serif">
                    {activeJob.status === 'queued'
                      ? 'Queued for generation on the server...'
                      : `Generating wiki — ${activeJob.progress.phase}`}
                    {activeJob.progress.pages_total > 0 &&
                      ` · ${activeJob.progress.pages_done}/${activeJob.progress.pages_total} pages`}
                    {jobElapsedSeconds > 0 && ` · ${Math.floor(jobElapsedSeconds / 60)}m ${jobElapsedSeconds % 60}s`}
                  </p>
                  <button
                    onClick={cancelActiveJob}
                    className="text-xs px-3 py-1.5 bg-[var(--background)] text-[var(--foreground)] rounded-md hover:bg-[var(--background)]/80 border border-[var(--border-color)] transition-colors hover:cursor-pointer flex-shrink-0"
                  >
                    {messages.common?.cancel || 'Cancel'}
                  </button>
                </div>
                <div className="bg-[var(--background)]/50 rounded-full h-2 mb-2 overflow-hidden border border-[var(--border-color)]">
                  <div
                    className="bg-[var(--accent-primary)] h-2 rounded-full transition-all duration-300 ease-in-out"
                    style={{
                      width: activeJob.progress.pages_total > 0
                        ? `${Math.max(5, 100 * activeJob.progress.pages_done / activeJob.progress.pages_total)}%`
                        : '5%'
                    }}
                  />
                </div>
                <p className="text-xs text-[var(--muted)]">
                  {activeJob.progress.current_page_title
                    ? `Writing: ${activeJob.progress.current_page_title}`
                    : 'Pages appear below as they are generated — you can leave this page; generation continues on the server.'}
                </p>
              </div>
            ) : (
              <div>
                <div className="flex items-center text-[var(--highlight)] mb-2">
                  <FaExclamationTriangle className="mr-2" />
                  <span className="font-bold font-serif text-sm">
                    {activeJob.status === 'failed' ? 'Generation failed'
                      : activeJob.status === 'cancelled' ? 'Generation cancelled'
                      : 'Generation interrupted by a server restart'}
                  </span>
                </div>
                {activeJob.error && (
                  <p className="text-[var(--foreground)] text-xs mb-3 break-words">{activeJob.error}</p>
                )}
                <p className="text-[var(--muted)] text-xs mb-3">
                  Pages generated before the stop are shown below (if any). Retry runs a fresh generation job.
                </p>
                <div className="flex gap-2">
                  <button onClick={retryJob} className="btn-japanese text-xs px-4 py-1.5 rounded-md">
                    {messages.common?.retry || 'Retry'}
                  </button>
                  <button
                    onClick={dismissActiveJob}
                    className="text-xs px-4 py-1.5 bg-[var(--background)] text-[var(--foreground)] rounded-md hover:bg-[var(--background)]/80 border border-[var(--border-color)] transition-colors"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {isLoading ? (
          <div className="flex flex-col items-center justify-center p-8 bg-[var(--card-bg)] rounded-lg shadow-custom card-japanese">
            <div className="relative mb-6">
              <div className="absolute -inset-4 bg-[var(--accent-primary)]/10 rounded-full blur-md animate-pulse"></div>
              <div className="relative flex items-center justify-center">
                <div className="w-3 h-3 bg-[var(--accent-primary)]/70 rounded-full animate-pulse"></div>
                <div className="w-3 h-3 bg-[var(--accent-primary)]/70 rounded-full animate-pulse delay-75 mx-2"></div>
                <div className="w-3 h-3 bg-[var(--accent-primary)]/70 rounded-full animate-pulse delay-150"></div>
              </div>
            </div>
            <p className="text-[var(--foreground)] text-center mb-3 font-serif">
              {loadingMessage || messages.common?.loading || 'Loading...'}
              {isExporting && (messages.loading?.preparingDownload || ' Please wait while we prepare your download...')}
            </p>
          </div>
        ) : error ? (
          <div className="bg-[var(--highlight)]/5 border border-[var(--highlight)]/30 rounded-lg p-5 mb-4 shadow-sm">
            <div className="flex items-center text-[var(--highlight)] mb-3">
              <FaExclamationTriangle className="mr-2" />
              <span className="font-bold font-serif">{messages.repoPage?.errorTitle || messages.common?.error || 'Error'}</span>
            </div>
            <p className="text-[var(--foreground)] text-sm mb-3">{error}</p>
            <p className="text-[var(--muted)] text-xs">
              {embeddingError ? (
                messages.repoPage?.embeddingErrorDefault || 'This error is related to the document embedding system used for analyzing your repository. Please verify your embedding model configuration, API keys, and try again. If the issue persists, consider switching to a different embedding provider in the model settings.'
              ) : (
                messages.repoPage?.errorMessageDefault || 'Please check that your repository exists and is public. Valid formats are "owner/repo", "https://github.com/owner/repo", "https://gitlab.com/owner/repo", "https://bitbucket.org/owner/repo", or local folder paths like "C:\\path\\to\\folder" or "/path/to/folder".'
              )}
            </p>
            <div className="mt-5">
              <Link
                href="/"
                className="btn-japanese px-5 py-2 inline-flex items-center gap-1.5"
              >
                <FaHome className="text-sm" />
                {messages.repoPage?.backToHome || 'Back to Home'}
              </Link>
            </div>
          </div>
        ) : wikiStructure ? (
          <div className="h-full overflow-y-auto flex flex-col lg:flex-row gap-4 w-full overflow-hidden bg-[var(--card-bg)] rounded-lg shadow-custom card-japanese">
            {/* Wiki Navigation */}
            <div className="h-full w-full lg:w-[280px] xl:w-[320px] flex-shrink-0 bg-[var(--background)]/50 rounded-lg rounded-r-none p-5 border-b lg:border-b-0 lg:border-r border-[var(--border-color)] overflow-y-auto">
              <h3 className="text-lg font-bold text-[var(--foreground)] mb-3 font-serif">{wikiStructure.title}</h3>
              <p className="text-[var(--muted)] text-sm mb-5 leading-relaxed">{wikiStructure.description}</p>

              {/* Display repository info */}
              <div className="text-xs text-[var(--muted)] mb-5 flex items-center">
                {effectiveRepoInfo.type === 'local' ? (
                  <div className="flex items-center">
                    <FaFolder className="mr-2" />
                    <span className="break-all">{effectiveRepoInfo.localPath}</span>
                  </div>
                ) : (
                  <>
                    {effectiveRepoInfo.type === 'github' ? (
                      <FaGithub className="mr-2" />
                    ) : effectiveRepoInfo.type === 'gitlab' ? (
                      <FaGitlab className="mr-2" />
                    ) : (
                      <FaBitbucket className="mr-2" />
                    )}
                    <a
                      href={effectiveRepoInfo.repoUrl ?? ''}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="hover:text-[var(--accent-primary)] transition-colors border-b border-[var(--border-color)] hover:border-[var(--accent-primary)]"
                    >
                      {effectiveRepoInfo.owner}/{effectiveRepoInfo.repo}
                    </a>
                  </>
                )}
              </div>

              {/* Wiki Type Indicator */}
              <div className="mb-3 flex items-center text-xs text-[var(--muted)]">
                <span className="mr-2">Wiki Type:</span>
                <span className={`px-2 py-0.5 rounded-full ${isComprehensiveView
                  ? 'bg-[var(--accent-primary)]/10 text-[var(--accent-primary)] border border-[var(--accent-primary)]/30'
                  : 'bg-[var(--background)] text-[var(--foreground)] border border-[var(--border-color)]'}`}>
                  {isComprehensiveView
                    ? (messages.form?.comprehensive || 'Comprehensive')
                    : (messages.form?.concise || 'Concise')}
                </span>
              </div>

              {/* Refresh Wiki button */}
              <div className="mb-5">
                <button
                  onClick={() => setIsModelSelectionModalOpen(true)}
                  disabled={isLoading}
                  className="flex items-center w-full text-xs px-3 py-2 bg-[var(--background)] text-[var(--foreground)] rounded-md hover:bg-[var(--background)]/80 disabled:opacity-50 disabled:cursor-not-allowed border border-[var(--border-color)] transition-colors hover:cursor-pointer"
                >
                  <FaSync className={`mr-2 ${isLoading ? 'animate-spin' : ''}`} />
                  {messages.repoPage?.refreshWiki || 'Refresh Wiki'}
                </button>
              </div>

              {/* Export buttons */}
              {Object.keys(generatedPages).length > 0 && (
                <div className="mb-5">
                  <h4 className="text-sm font-semibold text-[var(--foreground)] mb-3 font-serif">
                    {messages.repoPage?.exportWiki || 'Export Wiki'}
                  </h4>
                  <div className="flex flex-col gap-2">
                    <button
                      onClick={() => exportWiki('markdown')}
                      disabled={isExporting}
                      className="btn-japanese flex items-center text-xs px-3 py-2 rounded-md disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <FaDownload className="mr-2" />
                      {messages.repoPage?.exportAsMarkdown || 'Export as Markdown'}
                    </button>
                    <button
                      onClick={() => exportWiki('json')}
                      disabled={isExporting}
                      className="flex items-center text-xs px-3 py-2 bg-[var(--background)] text-[var(--foreground)] rounded-md hover:bg-[var(--background)]/80 disabled:opacity-50 disabled:cursor-not-allowed border border-[var(--border-color)] transition-colors"
                    >
                      <FaFileExport className="mr-2" />
                      {messages.repoPage?.exportAsJson || 'Export as JSON'}
                    </button>
                  </div>
                  {exportError && (
                    <div className="mt-2 text-xs text-[var(--highlight)]">
                      {exportError}
                    </div>
                  )}
                </div>
              )}

              {Object.keys(generatedPages).length > 0 && (
                <div className="mb-5">
                  <button
                    onClick={() => setIsReviewModalOpen(true)}
                    disabled={isLoading}
                    className="flex items-center w-full text-xs px-3 py-2 bg-[var(--background)] text-[var(--foreground)] rounded-md hover:bg-[var(--background)]/80 disabled:opacity-50 disabled:cursor-not-allowed border border-[var(--border-color)] transition-colors hover:cursor-pointer"
                  >
                    Model Review
                  </button>
                </div>
              )}

              <h4 className="text-md font-semibold text-[var(--foreground)] mb-3 font-serif">
                {messages.repoPage?.pages || 'Pages'}
              </h4>
              <WikiTreeView
                wikiStructure={wikiStructure}
                currentPageId={currentPageId}
                onPageSelect={handlePageSelect}
                messages={messages.repoPage}
              />
            </div>

            {/* Wiki Content */}
            <div id="wiki-content" className="w-full flex-grow p-6 lg:p-8 overflow-y-auto">
              {currentPageId && generatedPages[currentPageId] ? (
                <div className="max-w-[900px] xl:max-w-[1000px] mx-auto">
                  <h3 className="text-xl font-bold text-[var(--foreground)] mb-1 break-words font-serif">
                    {generatedPages[currentPageId].title}
                  </h3>

                  {/* Which model generated the wiki being displayed */}
                  {selectedProviderState && selectedModelState && (
                    <p className="text-xs text-[var(--muted)] mb-4">
                      Generated by {selectedProviderState}/{selectedModelState}
                      {wikiMeta.selfReviewed ? ' · self-reviewed' : ''}
                      {wikiMeta.generatedAt ? ` · ${new Date(wikiMeta.generatedAt).toLocaleString()}` : ''}
                    </p>
                  )}

                  <div className="prose prose-sm md:prose-base lg:prose-lg max-w-none">
                    <Markdown
                      content={generatedPages[currentPageId].content}
                      repoInfo={effectiveRepoInfo}
                    />
                  </div>

                  {generatedPages[currentPageId].relatedPages.length > 0 && (
                    <div className="mt-8 pt-4 border-t border-[var(--border-color)]">
                      <h4 className="text-sm font-semibold text-[var(--muted)] mb-3">
                        {messages.repoPage?.relatedPages || 'Related Pages:'}
                      </h4>
                      <div className="flex flex-wrap gap-2">
                        {generatedPages[currentPageId].relatedPages.map(relatedId => {
                          const relatedPage = wikiStructure.pages.find(p => p.id === relatedId);
                          return relatedPage ? (
                            <button
                              key={relatedId}
                              className="bg-[var(--accent-primary)]/10 hover:bg-[var(--accent-primary)]/20 text-xs text-[var(--accent-primary)] px-3 py-1.5 rounded-md transition-colors truncate max-w-full border border-[var(--accent-primary)]/20"
                              onClick={() => handlePageSelect(relatedId)}
                            >
                              {relatedPage.title}
                            </button>
                          ) : null;
                        })}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="flex flex-col items-center justify-center p-8 text-[var(--muted)] h-full">
                  <div className="relative mb-4">
                    <div className="absolute -inset-2 bg-[var(--accent-primary)]/5 rounded-full blur-md"></div>
                    <FaBookOpen className="text-4xl relative z-10" />
                  </div>
                  <p className="font-serif">
                    {messages.repoPage?.selectPagePrompt || 'Select a page from the navigation to view its content'}
                  </p>
                </div>
              )}
            </div>
          </div>
        ) : cacheMiss && !activeJob ? (
          /* Cache miss with no running job: offer generation, never auto-spend */
          <div className="flex flex-col items-center justify-center p-8 bg-[var(--card-bg)] rounded-lg shadow-custom card-japanese">
            <div className="relative mb-4">
              <div className="absolute -inset-2 bg-[var(--accent-primary)]/5 rounded-full blur-md"></div>
              <FaBookOpen className="text-4xl relative z-10 text-[var(--muted)]" />
            </div>
            <p className="font-serif text-[var(--foreground)] mb-2">
              No wiki has been generated for {effectiveRepoInfo.owner}/{effectiveRepoInfo.repo}
              {selectedProviderState && selectedModelState ? ` with ${selectedProviderState}/${selectedModelState}` : ''} yet.
            </p>
            <p className="text-xs text-[var(--muted)] mb-5">
              Generation runs on the server and can take a while; you can navigate away and come back.
            </p>
            <button onClick={startGeneration} className="btn-japanese px-5 py-2 inline-flex items-center gap-1.5">
              <FaSync className="text-sm" />
              Generate this wiki
            </button>
          </div>
        ) : null}
      </main>

      <footer className="max-w-[90%] xl:max-w-[1400px] mx-auto mt-8 flex flex-col gap-4 w-full">
        <div className="flex justify-between items-center gap-4 text-center text-[var(--muted)] text-sm h-fit w-full bg-[var(--card-bg)] rounded-lg p-3 shadow-sm border border-[var(--border-color)]">
          <p className="flex-1 font-serif">
            {messages.footer?.copyright || 'DeepWiki - Generate Wiki from GitHub/Gitlab/Bitbucket repositories'}
          </p>
          <ThemeToggle />
        </div>
      </footer>

      {/* Floating Chat Button */}
      {!isLoading && wikiStructure && (
        <button
          onClick={() => setIsAskModalOpen(true)}
          className="fixed bottom-6 right-6 w-14 h-14 rounded-full bg-[var(--accent-primary)] text-white shadow-lg flex items-center justify-center hover:bg-[var(--accent-primary)]/90 transition-all z-50"
          aria-label={messages.ask?.title || 'Ask about this repository'}
        >
          <FaComments className="text-xl" />
        </button>
      )}

      {/* Ask Modal - Always render but conditionally show/hide */}
      <div className={`fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4 transition-opacity duration-300 ${isAskModalOpen ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}>
        <div className="bg-[var(--card-bg)] rounded-lg shadow-xl w-full max-w-3xl max-h-[80vh] flex flex-col">
          <div className="flex items-center justify-end p-3 absolute top-0 right-0 z-10">
            <button
              onClick={() => {
                // Just close the modal without clearing the conversation
                setIsAskModalOpen(false);
              }}
              className="text-[var(--muted)] hover:text-[var(--foreground)] transition-colors bg-[var(--card-bg)]/80 rounded-full p-2"
              aria-label="Close"
            >
              <FaTimes className="text-xl" />
            </button>
          </div>
          <div className="flex-1 overflow-y-auto p-4">
            <Ask
              repoInfo={effectiveRepoInfo}
              provider={selectedProviderState}
              model={selectedModelState}
              isCustomModel={isCustomSelectedModelState}
              customModel={customSelectedModelState}
              language={language}
              onRef={(ref) => (askComponentRef.current = ref)}
            />
          </div>
        </div>
      </div>

      <ModelSelectionModal
        isOpen={isModelSelectionModalOpen}
        onClose={() => setIsModelSelectionModalOpen(false)}
        provider={selectedProviderState}
        setProvider={setSelectedProviderState}
        model={selectedModelState}
        setModel={setSelectedModelState}
        isCustomModel={isCustomSelectedModelState}
        setIsCustomModel={setIsCustomSelectedModelState}
        customModel={customSelectedModelState}
        setCustomModel={setCustomSelectedModelState}
        isComprehensiveView={isComprehensiveView}
        setIsComprehensiveView={setIsComprehensiveView}
        isSelfReviewEnabled={isSelfReviewEnabled}
        setIsSelfReviewEnabled={setIsSelfReviewEnabled}
        showFileFilters={true}
        excludedDirs={modelExcludedDirs}
        setExcludedDirs={setModelExcludedDirs}
        excludedFiles={modelExcludedFiles}
        setExcludedFiles={setModelExcludedFiles}
        includedDirs={modelIncludedDirs}
        setIncludedDirs={setModelIncludedDirs}
        includedFiles={modelIncludedFiles}
        setIncludedFiles={setModelIncludedFiles}
        onApply={(token?: string) => confirmRefresh(token, false)}
        onForceRegenerate={(token?: string, provider?: string, model?: string) => confirmRefresh(token, true, provider, model)}
        showWikiType={true}
        showTokenInput={effectiveRepoInfo.type !== 'local' && !currentToken} // Show token input if not local and no current token
        repositoryType={effectiveRepoInfo.type as 'github' | 'gitlab' | 'bitbucket'}
        authRequired={authRequired}
        authCode={authCode}
        setAuthCode={setAuthCode}
        isAuthLoading={isAuthLoading}
      />
      <WikiReviewModal
        isOpen={isReviewModalOpen}
        onClose={() => setIsReviewModalOpen(false)}
        repoInfo={effectiveRepoInfo}
        language={language}
        pages={wikiStructure ? wikiStructure.pages.map(p => generatedPages[p.id]).filter((p): p is WikiPage => Boolean(p)) : []}
        reviewedProvider={selectedProviderState}
        reviewedModel={selectedModelState}
        token={currentToken}
        onPagesRevised={handlePagesRevised}
      />
    </div>
  );
}
