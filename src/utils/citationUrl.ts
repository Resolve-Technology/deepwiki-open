import RepoInfo from '@/types/repoinfo';

export interface ParsedCitation {
  filePath: string;
  startLine?: number;
  endLine?: number;
}

// "path/to/file.ext" with an optional ":12" or ":12-34" suffix.
// Requires a file extension so ordinary link labels don't match.
const CITATION_RE = /^(.+?\.[A-Za-z0-9]+)(?::(\d+)(?:-(\d+))?)?$/;

export function parseCitation(text: string): ParsedCitation | null {
  const m = CITATION_RE.exec(text.trim());
  if (!m) return null;
  const [, filePath, start, end] = m;
  const out: ParsedCitation = { filePath };
  if (start) out.startLine = parseInt(start, 10);
  if (end) out.endLine = parseInt(end, 10);
  return out;
}

export function buildBlobUrl(repoInfo: RepoInfo, filePath: string, branch: string): string | null {
  if (repoInfo.type === 'local' || !repoInfo.repoUrl) return null;
  const base = repoInfo.repoUrl.replace(/\.git$/, '').replace(/\/+$/, '');
  switch (repoInfo.type) {
    case 'github': return `${base}/blob/${branch}/${filePath}`;
    case 'gitlab': return `${base}/-/blob/${branch}/${filePath}`;
    case 'bitbucket': return `${base}/src/${branch}/${filePath}`;
    default: return null;
  }
}

export function lineAnchor(repoType: string, start?: number, end?: number): string {
  if (!start) return '';
  const range = end && end !== start;
  switch (repoType) {
    case 'github': return range ? `#L${start}-L${end}` : `#L${start}`;
    case 'gitlab': return range ? `#L${start}-${end}` : `#L${start}`;
    // bitbucket + unknown: file-level link only (anchor format unverified)
    default: return '';
  }
}

const BRANCH_RE = /\/(?:-\/blob|blob|src)\/([^/#?]+)\//;

export function extractDefaultBranch(content: string): string {
  const m = BRANCH_RE.exec(content);
  return m ? m[1] : 'main';
}

export function buildCitationHref(repoInfo: RepoInfo, branch: string, text: string): string | null {
  const cite = parseCitation(text);
  if (!cite) return null;
  const url = buildBlobUrl(repoInfo, cite.filePath, branch);
  if (!url) return null;
  return url + lineAnchor(repoInfo.type, cite.startLine, cite.endLine);
}
