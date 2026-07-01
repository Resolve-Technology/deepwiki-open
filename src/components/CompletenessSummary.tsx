import React from 'react';

interface DocCounts {
  headings: { content: number; empty_none: number; missing: number };
  rows: { present: number; missing: number };
}

export interface CompletenessReport {
  summary: {
    headings: { content: number; empty_none: number; missing: number };
    rows: { present: number; missing: number };
    pages_missing: string[];
    by_document?: { TSD: DocCounts; BRD: DocCounts };
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

const DocLine: React.FC<{ label: string; d: DocCounts; showRows?: boolean }> = ({ label, d, showRows }) => {
  const h = d.headings;
  const rowTotal = d.rows.present + d.rows.missing;
  return (
    <div>
      {label}: {h.content} ok / {h.empty_none} None{' / '}
      <span className={h.missing > 0 ? 'text-red-600 font-medium' : ''}>{h.missing} ✗</span>
      {showRows && rowTotal > 0 && (
        <>
          {'  ·  '}
          <span className={d.rows.missing > 0 ? 'text-red-600 font-medium' : ''}>
            Impact rows: {d.rows.present}/{rowTotal}
          </span>
        </>
      )}
    </div>
  );
};

// Prop-driven: renders the completeness summary + link, or nothing when the
// report is absent. The parent fetches the report and builds mdHref.
export const CompletenessSummary: React.FC<{
  report: CompletenessReport | null;
  mdHref: string;
}> = ({ report, mdHref }) => {
  if (!report) return null;
  const bd = report.summary.by_document;
  const link = (
    <a href={mdHref} target="_blank" rel="noopener noreferrer"
       className="text-[var(--accent-primary)] hover:underline">view full report ↗</a>
  );
  if (bd) {
    return (
      <div className="mt-1 text-[11px] text-[var(--muted)]">
        {/* Only TSD has enumerated rows today (Impact Analysis is the sole
            ENUMERATED_SECTIONS entry); showRows is passed to the TSD line only. */}
        <DocLine label="TSD" d={bd.TSD} showRows />
        <DocLine label="BRD" d={bd.BRD} />
        <div>{link}</div>
      </div>
    );
  }
  const h = report.summary.headings;
  return (
    <div className="mt-1 text-[11px] text-[var(--muted)]">
      Completeness: {h.content} ok / {h.empty_none} None{' / '}
      <span className={h.missing > 0 ? 'text-red-600 font-medium' : ''}>{h.missing} ✗</span>
      {' · '}{link}
    </div>
  );
};
