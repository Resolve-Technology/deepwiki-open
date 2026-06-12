import { describe, it, expect, vi } from 'vitest';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import RepoInfo from '@/types/repoinfo';

// Mermaid pulls in browser-only deps at import time; stub it so Markdown can
// render in a plain Node test environment.
vi.mock('./Mermaid', () => ({ default: () => null }));

import Markdown from './Markdown';

const gitlab: RepoInfo = {
  owner: 'poc', repo: 'code2_sqlcbl_cal101', type: 'gitlab', token: null,
  localPath: null, repoUrl: 'https://gitlab.reslv.one/poc/code2_sqlcbl_cal101.git',
};
const local: RepoInfo = {
  owner: 'local', repo: 'x', type: 'local', token: null,
  localPath: '/root/.adalflow/repos/x', repoUrl: null,
};

const render = (content: string, repoInfo?: RepoInfo, citations?: Record<string, unknown>) =>
  renderToStaticMarkup(
    React.createElement(Markdown, { content, repoInfo, citations } as React.ComponentProps<typeof Markdown>),
  );

describe('Markdown source citations', () => {
  it('rewrites an empty-href citation into a gitlab blob link with line anchor (and strips .git)', () => {
    const html = render('Sources: [CAL101.txt:51-54]()', gitlab);
    expect(html).toContain(
      'href="https://gitlab.reslv.one/poc/code2_sqlcbl_cal101/-/blob/main/CAL101.txt#L51-54"',
    );
    expect(html).not.toContain('.git/-/blob');
  });

  it('renders a citation as plain text (no link) for a local repo', () => {
    const html = render('Sources: [CAL101.txt:51-54]()', local);
    expect(html).toContain('<span');
    expect(html).toContain('CAL101.txt:51-54');
    // No anchor element wrapping the citation text.
    expect(html).not.toMatch(/<a[^>]*>CAL101\.txt:51-54<\/a>/);
  });

  it('leaves a real link untouched', () => {
    const html = render('See [the repo](https://gitlab.reslv.one/poc/code2_sqlcbl_cal101)', gitlab);
    expect(html).toContain('href="https://gitlab.reslv.one/poc/code2_sqlcbl_cal101"');
  });
});

describe('Markdown citation grounding', () => {
  it('verified citation with snippet shows the source text, no link', () => {
    const citations = {
      'CAL101.txt:51-54': {
        status: 'verified', filePath: 'CAL101.txt',
        startLine: 51, endLine: 54, snippet: 'MOVE A TO B',
      },
    };
    const html = render('Sources: [CAL101.txt:51-54]()', gitlab, citations);
    expect(html).toContain('MOVE A TO B');          // real source text inlined
    expect(html).not.toContain('/-/blob/');         // no gitlab link
    expect(html).toContain('✓');                    // rendered as a verified badge
  });

  it('verified whole-file citation shows a badge, no link', () => {
    const citations = {
      'CAL101.txt': { status: 'verified', filePath: 'CAL101.txt' },
    };
    const html = render('Sources: [CAL101.txt]()', gitlab, citations);
    expect(html).toContain('✓');                    // verified badge, not a link
    expect(html).not.toMatch(/<a[^>]*>/);           // no anchor element at all
    expect(html).not.toContain('/-/blob/');
  });

  it('broken citation shows a red unverified marker, no link', () => {
    const citations = {
      'GHOST.txt:1-5': {
        status: 'broken', filePath: 'GHOST.txt',
        startLine: 1, endLine: 5, reason: 'file not provided',
      },
    };
    const html = render('Sources: [GHOST.txt:1-5]()', gitlab, citations);
    expect(html).toContain('unverified');
    expect(html).not.toContain('/-/blob/');
  });

  it('citation absent from the map falls back to a blob link (regression)', () => {
    const html = render('Sources: [CAL101.txt:51-54]()', gitlab);
    expect(html).toContain(
      'href="https://gitlab.reslv.one/poc/code2_sqlcbl_cal101/-/blob/main/CAL101.txt#L51-54"',
    );
  });
});
