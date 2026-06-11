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

const render = (content: string, repoInfo?: RepoInfo) =>
  renderToStaticMarkup(React.createElement(Markdown, { content, repoInfo }));

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
