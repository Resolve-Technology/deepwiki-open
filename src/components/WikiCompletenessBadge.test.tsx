import { describe, it, expect } from 'vitest';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { WikiCompletenessBadge } from './WikiCompletenessBadge';

const repoInfo = { owner: 'poc', repo: 'code1_cbl_bv401', type: 'gitlab' };

describe('WikiCompletenessBadge', () => {
  it('renders nothing when provider/model are empty', () => {
    const html = renderToStaticMarkup(
      React.createElement(WikiCompletenessBadge,
        { repoInfo, language: 'zh-tw', provider: '', model: '' }));
    expect(html).toBe('');
  });
});
