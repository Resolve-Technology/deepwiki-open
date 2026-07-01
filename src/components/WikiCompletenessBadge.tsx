'use client';

import React, { useEffect, useState } from 'react';
import { CompletenessSummary, completenessQuery, CompletenessReport } from './CompletenessSummary';

// Fetches the completeness report for the currently-displayed wiki version and
// renders the shared CompletenessSummary. Renders nothing when provider/model
// are unknown, the fetch fails, or no report exists — never blocks the page.
export const WikiCompletenessBadge: React.FC<{
  repoInfo: { owner: string; repo: string; type: string };
  language: string;
  provider: string;
  model: string;
}> = ({ repoInfo, language, provider, model }) => {
  const [report, setReport] = useState<CompletenessReport | null>(null);

  const query = provider && model
    ? completenessQuery({ repo: repoInfo, language, provider, model })
    : '';

  useEffect(() => {
    setReport(null);   // clear any stale report immediately on any query change
    if (!query) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/wiki_completeness?${query}&format=json`);
        const data = res.ok ? await res.json() : null;
        if (!cancelled) setReport(data as CompletenessReport | null);
      } catch {
        if (!cancelled) setReport(null);
      }
    })();
    return () => { cancelled = true; };
  }, [query]);

  if (!query) return null;
  return (
    <CompletenessSummary
      report={report}
      mdHref={`/api/wiki_completeness?${query}&format=md`}
    />
  );
};
