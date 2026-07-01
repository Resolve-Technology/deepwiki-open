import React from 'react';

export interface CompletenessReport {
  summary: {
    headings: { content: number; empty_none: number; missing: number };
    rows: { present: number; missing: number };
    pages_missing: string[];
  };
}

export function completenessQuery(job: {
  repo: { owner: string; repo: string; type: string };
  language: string;
  provider: string;
  model: string;
}): string {
  return new URLSearchParams({
    owner: job.repo.owner,
    repo: job.repo.repo,
    repo_type: job.repo.type,
    language: job.language,
    provider: job.provider,
    model: job.model,
  }).toString();
}

// Prop-driven: renders the completeness summary + link, or nothing when the
// report is absent. The parent fetches the report and builds mdHref.
export const CompletenessSummary: React.FC<{
  report: CompletenessReport | null;
  mdHref: string;
}> = ({ report, mdHref }) => {
  if (!report) return null;
  const h = report.summary.headings;
  return (
    <div className="mt-1 text-[11px] text-[var(--muted)]">
      Completeness: {h.content} ok / {h.empty_none} None{' / '}
      <span className={h.missing > 0 ? 'text-red-600 font-medium' : ''}>{h.missing} ✗</span>
      {' · '}
      <a
        href={mdHref}
        target="_blank"
        rel="noopener noreferrer"
        className="text-[var(--accent-primary)] hover:underline"
      >
        view full report ↗
      </a>
    </div>
  );
};
