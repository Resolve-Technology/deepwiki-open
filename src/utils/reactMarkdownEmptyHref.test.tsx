import { describe, it, expect } from 'vitest';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ReactMarkdown from 'react-markdown';

// Foundational gate: the clickable-citation feature relies on react-markdown
// turning `[text]()` (empty link destination) into a link element whose label
// carries the citation text. If this stops holding, the render-time rewrite in
// Markdown.tsx cannot work. Guaranteed by CommonMark, verified explicitly here.
describe('react-markdown empty-href links', () => {
  it('renders [text]() as an <a> element carrying the text', () => {
    const html = renderToStaticMarkup(
      React.createElement(ReactMarkdown, null, '[CAL101.txt:51-54]()'),
    );
    expect(html).toMatch(/<a\b/);
    expect(html).toContain('CAL101.txt:51-54');
  });
});
