'use client';

import React, { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';

// Matches the public dict served by /api/wiki_jobs
interface WikiJob {
  id: string;
  repo: { owner: string; repo: string; type: string; repoUrl?: string | null; localPath?: string | null };
  language: string;
  provider: string;
  model: string;
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'interrupted';
  progress: { phase: string; pages_total: number; pages_done: number; current_page_title: string };
  error?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

const ACTIVE_STATUSES = ['queued', 'running'];
const ACTIVE_POLL_MS = 5000;
const IDLE_POLL_MS = 30000;
const FINISHED_WINDOW_MS = 60 * 60 * 1000; // show finished jobs from the last hour

const statusBadgeClasses: Record<string, string> = {
  queued: 'bg-[var(--background)] text-[var(--muted)] border-[var(--border-color)]',
  running: 'bg-[var(--accent-primary)]/10 text-[var(--accent-primary)] border-[var(--accent-primary)]/30',
  done: 'bg-green-500/10 text-green-600 border-green-500/30',
  failed: 'bg-[var(--highlight)]/10 text-[var(--highlight)] border-[var(--highlight)]/30',
  cancelled: 'bg-[var(--background)] text-[var(--muted)] border-[var(--border-color)]',
  interrupted: 'bg-[var(--highlight)]/10 text-[var(--highlight)] border-[var(--highlight)]/30',
};

function jobWikiUrl(job: WikiJob): string {
  const params = new URLSearchParams();
  params.append('type', job.repo.type);
  if (job.repo.type === 'local' && job.repo.localPath) {
    params.append('local_path', encodeURIComponent(job.repo.localPath));
  } else if (job.repo.repoUrl) {
    params.append('repo_url', encodeURIComponent(job.repo.repoUrl));
  }
  if (job.provider) params.append('provider', job.provider);
  if (job.model) params.append('model', job.model);
  params.append('language', job.language);
  return `/${job.repo.owner}/${job.repo.repo}?${params.toString()}`;
}

interface JobsPanelProps {
  authCode?: string;
  className?: string;
}

export default function JobsPanel({ authCode, className = '' }: JobsPanelProps) {
  const [jobs, setJobs] = useState<WikiJob[]>([]);

  const fetchJobs = useCallback(async () => {
    try {
      const response = await fetch('/api/wiki_jobs');
      if (!response.ok) return;
      const data = await response.json();
      setJobs(data.jobs || []);
    } catch (err) {
      console.error('Error fetching wiki jobs:', err);
    }
  }, []);

  // Poll faster while anything is queued/running, slowly otherwise
  const hasActive = jobs.some(j => ACTIVE_STATUSES.includes(j.status));
  useEffect(() => {
    fetchJobs();
    const interval = setInterval(fetchJobs, hasActive ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    return () => clearInterval(interval);
  }, [fetchJobs, hasActive]);

  const cancelJob = useCallback(async (jobId: string) => {
    try {
      const params = authCode ? `?authorization_code=${encodeURIComponent(authCode)}` : '';
      await fetch(`/api/wiki_jobs/${jobId}/cancel${params}`, { method: 'POST' });
      fetchJobs();
    } catch (err) {
      console.error('Error cancelling wiki job:', err);
    }
  }, [authCode, fetchJobs]);

  // Dismiss a finished job: removed server-side, so it stays gone on reload
  const removeJob = useCallback(async (jobId: string) => {
    try {
      const params = authCode ? `?authorization_code=${encodeURIComponent(authCode)}` : '';
      await fetch(`/api/wiki_jobs/${jobId}${params}`, { method: 'DELETE' });
      fetchJobs();
    } catch (err) {
      console.error('Error removing wiki job:', err);
    }
  }, [authCode, fetchJobs]);

  const activeJobs = jobs.filter(j => ACTIVE_STATUSES.includes(j.status));
  const recentFinished = jobs.filter(j =>
    !ACTIVE_STATUSES.includes(j.status) &&
    j.finished_at &&
    Date.now() - new Date(j.finished_at).getTime() < FINISHED_WINDOW_MS);

  if (activeJobs.length === 0 && recentFinished.length === 0) {
    return null;
  }

  return (
    <div className={`bg-[var(--card-bg)] rounded-lg shadow-custom card-japanese p-4 ${className}`}>
      <h3 className="text-sm font-bold text-[var(--foreground)] mb-3 font-serif">
        Wiki generation jobs
      </h3>

      {activeJobs.length > 0 && (
        <ul className="space-y-3 mb-2">
          {activeJobs.map(job => (
            <li key={job.id} className="text-xs">
              <div className="flex items-center justify-between gap-3 mb-1">
                <Link href={jobWikiUrl(job)} className="text-[var(--accent-primary)] hover:text-[var(--highlight)] truncate font-medium">
                  {job.repo.owner}/{job.repo.repo}
                </Link>
                <span className="text-[var(--muted)] truncate">
                  {job.provider}/{job.model} · {job.status === 'queued' ? 'queued' : job.progress.phase}
                  {job.progress.pages_total > 0 && ` · ${job.progress.pages_done}/${job.progress.pages_total}`}
                </span>
                <button
                  onClick={() => cancelJob(job.id)}
                  className="px-2 py-0.5 rounded border border-[var(--border-color)] text-[var(--muted)] hover:text-[var(--foreground)] hover:bg-[var(--background)] transition-colors flex-shrink-0"
                >
                  Cancel
                </button>
              </div>
              <div className="bg-[var(--background)]/50 rounded-full h-1.5 overflow-hidden border border-[var(--border-color)]">
                <div
                  className="bg-[var(--accent-primary)] h-1.5 rounded-full transition-all duration-300 ease-in-out"
                  style={{
                    width: job.progress.pages_total > 0
                      ? `${Math.max(4, 100 * job.progress.pages_done / job.progress.pages_total)}%`
                      : '4%'
                  }}
                />
              </div>
            </li>
          ))}
        </ul>
      )}

      {recentFinished.length > 0 && (
        <ul className="space-y-1">
          {recentFinished.map(job => (
            <li key={job.id} className="flex items-center justify-between gap-3 text-xs">
              <Link href={jobWikiUrl(job)} className="text-[var(--foreground)] hover:text-[var(--accent-primary)] truncate">
                {job.repo.owner}/{job.repo.repo}
                <span className="text-[var(--muted)]"> · {job.provider}/{job.model}</span>
              </Link>
              <span className="flex items-center gap-1.5 flex-shrink-0">
                <span
                  className={`px-2 py-0.5 rounded-full border ${statusBadgeClasses[job.status] || statusBadgeClasses.queued}`}
                  title={job.error || undefined}
                >
                  {job.status}
                </span>
                <button
                  onClick={() => removeJob(job.id)}
                  title="Remove from this list"
                  aria-label="Remove job"
                  className="px-1.5 py-0.5 rounded border border-[var(--border-color)] text-[var(--muted)] hover:text-[var(--foreground)] hover:bg-[var(--background)] transition-colors leading-none"
                >
                  ×
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
