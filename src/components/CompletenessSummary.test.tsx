import { describe, it, expect } from 'vitest';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { CompletenessSummary, completenessQuery, CompletenessReport } from './CompletenessSummary';

const report: CompletenessReport = {
  summary: { headings: { content: 45, empty_none: 6, missing: 1 },
             rows: { present: 21, missing: 1 }, pages_missing: [] },
};

describe('CompletenessSummary', () => {
  it('renders the summary counts and a view-full-report link', () => {
    const html = renderToStaticMarkup(
      React.createElement(CompletenessSummary, { report, mdHref: '/api/wiki_completeness?x=1&format=md' }));
    expect(html).toContain('45 ok / 6 None /');
    expect(html).toContain('1 ✗');
    expect(html).toContain('view full report');
    expect(html).toContain('format=md');
    expect(html).toMatch(/text-red-600/);          // missing>0 styled red
  });

  it('renders nothing when there is no report', () => {
    const html = renderToStaticMarkup(
      React.createElement(CompletenessSummary, { report: null, mdHref: '/x' }));
    expect(html).toBe('');
  });

  it('does not style the missing count red when zero', () => {
    const clean: CompletenessReport = {
      summary: { headings: { content: 50, empty_none: 0, missing: 0 },
                 rows: { present: 22, missing: 0 }, pages_missing: [] } };
    const html = renderToStaticMarkup(
      React.createElement(CompletenessSummary, { report: clean, mdHref: '/x?format=md' }));
    expect(html).toContain('0 ✗');
    expect(html).not.toMatch(/text-red-600/);
  });

  it('completenessQuery builds the expected params', () => {
    const q = completenessQuery({
      repo: { owner: 'poc', repo: 'code1_cbl_bv401', type: 'gitlab' },
      language: 'zh-tw', provider: 'claude', model: 'claude-haiku-4-5-20251001' });
    expect(q).toContain('owner=poc');
    expect(q).toContain('repo=code1_cbl_bv401');
    expect(q).toContain('repo_type=gitlab');
    expect(q).toContain('language=zh-tw');
    expect(q).toContain('provider=claude');
    expect(q).toContain('model=claude-haiku-4-5-20251001');
  });
});
