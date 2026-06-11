import { describe, it, expect } from 'vitest';
import {
  parseCitation,
  buildBlobUrl,
  lineAnchor,
  extractDefaultBranch,
  buildCitationHref,
} from '@/utils/citationUrl';
import RepoInfo from '@/types/repoinfo';

const gitlab: RepoInfo = {
  owner: 'poc', repo: 'code2_sqlcbl_cal101', type: 'gitlab', token: null,
  localPath: null, repoUrl: 'https://gitlab.reslv.one/poc/code2_sqlcbl_cal101.git',
};
const github: RepoInfo = {
  owner: 'o', repo: 'r', type: 'github', token: null, localPath: null,
  repoUrl: 'https://github.com/o/r',
};
const local: RepoInfo = {
  owner: 'local', repo: 'x', type: 'local', token: null,
  localPath: '/root/.adalflow/repos/x', repoUrl: null,
};

describe('parseCitation', () => {
  it('parses a line range', () => {
    expect(parseCitation('CAL101.txt:51-54')).toEqual({ filePath: 'CAL101.txt', startLine: 51, endLine: 54 });
  });
  it('parses a single line', () => {
    expect(parseCitation('copybook/CLNMSKM.txt:12')).toEqual({ filePath: 'copybook/CLNMSKM.txt', startLine: 12 });
  });
  it('parses a whole-file citation', () => {
    expect(parseCitation('README.md')).toEqual({ filePath: 'README.md' });
  });
  it('returns null for non-citation text', () => {
    expect(parseCitation('see the overview page')).toBeNull();
  });
});

describe('buildBlobUrl', () => {
  it('builds a gitlab URL and strips .git', () => {
    expect(buildBlobUrl(gitlab, 'CAL101.txt', 'main'))
      .toBe('https://gitlab.reslv.one/poc/code2_sqlcbl_cal101/-/blob/main/CAL101.txt');
  });
  it('builds a github URL', () => {
    expect(buildBlobUrl(github, 'src/a.ts', 'develop'))
      .toBe('https://github.com/o/r/blob/develop/src/a.ts');
  });
  it('returns null for a local repo', () => {
    expect(buildBlobUrl(local, 'CAL101.txt', 'main')).toBeNull();
  });
  it('returns null when repoUrl is missing', () => {
    expect(buildBlobUrl({ ...github, repoUrl: null }, 'a.ts', 'main')).toBeNull();
  });
});

describe('lineAnchor', () => {
  it('github range uses L on both ends', () => {
    expect(lineAnchor('github', 51, 54)).toBe('#L51-L54');
  });
  it('gitlab range omits L on the end', () => {
    expect(lineAnchor('gitlab', 51, 54)).toBe('#L51-54');
  });
  it('single line is #L<n>', () => {
    expect(lineAnchor('gitlab', 51)).toBe('#L51');
    expect(lineAnchor('github', 51)).toBe('#L51');
  });
  it('no anchor without a start line', () => {
    expect(lineAnchor('gitlab')).toBe('');
  });
  it('bitbucket and unknown types get no line anchor', () => {
    expect(lineAnchor('bitbucket', 51, 54)).toBe('');
  });
});

describe('extractDefaultBranch', () => {
  it('reads the branch from a gitlab blob URL', () => {
    expect(extractDefaultBranch('x [f](https://gitlab.reslv.one/p/r/-/blob/release/CAL101.txt) y'))
      .toBe('release');
  });
  it('reads the branch from a github blob URL', () => {
    expect(extractDefaultBranch('[f](https://github.com/o/r/blob/main/a.ts)')).toBe('main');
  });
  it('tolerates a .git-suffixed URL (pre-fix cached pages)', () => {
    expect(extractDefaultBranch('[f](https://gitlab.reslv.one/p/r.git/-/blob/main/a.txt)')).toBe('main');
  });
  it('falls back to main when no blob URL is present', () => {
    expect(extractDefaultBranch('no links here')).toBe('main');
  });
});

describe('buildCitationHref', () => {
  it('composes URL + anchor for gitlab', () => {
    expect(buildCitationHref(gitlab, 'main', 'CAL101.txt:51-54'))
      .toBe('https://gitlab.reslv.one/poc/code2_sqlcbl_cal101/-/blob/main/CAL101.txt#L51-54');
  });
  it('returns null for a local repo', () => {
    expect(buildCitationHref(local, 'main', 'CAL101.txt:51-54')).toBeNull();
  });
  it('returns null for non-citation text', () => {
    expect(buildCitationHref(gitlab, 'main', 'just words')).toBeNull();
  });
});
