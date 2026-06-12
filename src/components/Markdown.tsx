import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeRaw from 'rehype-raw';
import rehypeKatex from 'rehype-katex';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { tomorrow } from 'react-syntax-highlighter/dist/cjs/styles/prism';
import Mermaid from './Mermaid';
import 'katex/dist/katex.min.css';
import RepoInfo from '@/types/repoinfo';
import { CitationInfo } from '@/types/wiki/wikipage';
import { parseCitation, buildBlobUrl, lineAnchor, extractDefaultBranch } from '@/utils/citationUrl';

// A verified citation: shows the cited filename, expandable to the real source
// text we provided the model. No external link — the text IS the evidence.
const CitationSnippet: React.FC<{ label: string; snippet?: string }> = ({ label, snippet }) => {
  const [open, setOpen] = React.useState(false);
  const badge = "text-green-700 dark:text-green-400 font-medium hover:underline";
  if (!snippet) {
    return <span className={badge}>✓ {label}</span>;
  }
  return (
    <span className="citation-verified">
      <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open} className={badge}>
        ✓ {label}
      </button>
      <span
        className={`block font-mono text-xs whitespace-pre overflow-x-auto my-1 p-2 rounded bg-gray-100 dark:bg-gray-800 ${open ? '' : 'hidden'}`}
      >
        {snippet}
      </span>
    </span>
  );
};

// A broken citation: the cited file/lines were not in the source we gave the
// model, so the claim may be fabricated.
const BrokenCitation: React.FC<{ label: string; reason?: string }> = ({ label, reason }) => (
  <span title={reason} className="text-red-600 dark:text-red-400 font-medium">
    ⚠ {label} — unverified
  </span>
);

// Flatten a react-markdown link's children to plain text, descending through
// inline wrapper elements (e.g. a bolded citation). Returns null only for
// genuinely non-textual content.
function nodeToPlainText(node: React.ReactNode): string | null {
  if (typeof node === 'string') return node;
  if (typeof node === 'number') return String(node);
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (Array.isArray(node)) {
    const parts = node.map(nodeToPlainText);
    return parts.some((p) => p === null) ? null : parts.join('');
  }
  if (React.isValidElement(node)) {
    return nodeToPlainText((node.props as { children?: React.ReactNode }).children);
  }
  return null;
}

interface MarkdownProps {
  content: string;
  repoInfo?: RepoInfo;
  citations?: Record<string, CitationInfo>;
}

const Markdown: React.FC<MarkdownProps> = ({ content, repoInfo, citations }) => {
  const defaultBranch = React.useMemo(() => extractDefaultBranch(content), [content]);
  // Define markdown components
  const MarkdownComponents: React.ComponentProps<typeof ReactMarkdown>['components'] = {
    p({ children, ...props }: { children?: React.ReactNode }) {
      return <p className="mb-3 text-sm leading-relaxed dark:text-white" {...props}>{children}</p>;
    },
    h1({ children, ...props }: { children?: React.ReactNode }) {
      return <h1 className="text-xl font-bold mt-6 mb-3 dark:text-white" {...props}>{children}</h1>;
    },
    h2({ children, ...props }: { children?: React.ReactNode }) {
      // Special styling for ReAct headings
      if (children && typeof children === 'string') {
        const text = children.toString();
        if (text.includes('Thought') || text.includes('Action') || text.includes('Observation') || text.includes('Answer')) {
          return (
            <h2
              className={`text-base font-bold mt-5 mb-3 p-2 rounded ${
                text.includes('Thought') ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-800 dark:text-blue-300' :
                text.includes('Action') ? 'bg-green-100 dark:bg-green-900/30 text-green-800 dark:text-green-300' :
                text.includes('Observation') ? 'bg-amber-100 dark:bg-amber-900/30 text-amber-800 dark:text-amber-300' :
                text.includes('Answer') ? 'bg-purple-100 dark:bg-purple-900/30 text-purple-800 dark:text-purple-300' :
                'dark:text-white'
              }`}
              {...props}
            >
              {children}
            </h2>
          );
        }
      }
      return <h2 className="text-lg font-bold mt-5 mb-3 dark:text-white" {...props}>{children}</h2>;
    },
    h3({ children, ...props }: { children?: React.ReactNode }) {
      return <h3 className="text-base font-semibold mt-4 mb-2 dark:text-white" {...props}>{children}</h3>;
    },
    h4({ children, ...props }: { children?: React.ReactNode }) {
      return <h4 className="text-sm font-semibold mt-3 mb-2 dark:text-white" {...props}>{children}</h4>;
    },
    ul({ children, ...props }: { children?: React.ReactNode }) {
      return <ul className="list-disc pl-6 mb-4 text-sm dark:text-white space-y-2" {...props}>{children}</ul>;
    },
    ol({ children, ...props }: { children?: React.ReactNode }) {
      return <ol className="list-decimal pl-6 mb-4 text-sm dark:text-white space-y-2" {...props}>{children}</ol>;
    },
    li({ children, ...props }: { children?: React.ReactNode }) {
      return <li className="mb-2 text-sm leading-relaxed dark:text-white" {...props}>{children}</li>;
    },
    a({ children, href, ...props }: { children?: React.ReactNode; href?: string }) {
      const linkClass = "text-purple-600 dark:text-purple-400 hover:underline font-medium";
      // react-markdown passes a hast `node` in props; don't spread it onto the DOM.
      const { node: _node, ...rest } = props as { node?: unknown };

      // Real link (e.g. the top-of-page <details> blob links) — leave untouched.
      if (href) {
        return (
          <a href={href} className={linkClass} target="_blank" rel="noopener noreferrer" {...rest}>
            {children}
          </a>
        );
      }

      // Empty href: maybe a "Sources" citation. Get the plain text label.
      const text = nodeToPlainText(children);
      const cite = text ? parseCitation(text) : null;
      const info = text ? citations?.[text] : undefined;
      if (text && info) {
        if (info.status === 'verified') {
          return <CitationSnippet label={text} snippet={info.snippet} />;
        }
        return <BrokenCitation label={text} reason={info.reason} />;
      }
      if (text && cite && repoInfo) {
        const url = buildBlobUrl(repoInfo, cite.filePath, defaultBranch);
        if (url) {
          const finalHref = url + lineAnchor(repoInfo.type, cite.startLine, cite.endLine);
          return (
            <a href={finalHref} className={linkClass} target="_blank" rel="noopener noreferrer" {...rest}>
              {children}
            </a>
          );
        }
        // Citation, but no buildable URL (local repo / no repoUrl) → plain text, not a dead link.
        return (
          <span className="text-gray-500 dark:text-gray-400 font-medium" {...rest}>
            {children}
          </span>
        );
      }

      // Not a citation (or unstringifiable) → preserve previous behavior.
      return (
        <a href={href} className={linkClass} target="_blank" rel="noopener noreferrer" {...rest}>
          {children}
        </a>
      );
    },
    blockquote({ children, ...props }: { children?: React.ReactNode }) {
      return (
        <blockquote
          className="border-l-4 border-gray-300 dark:border-gray-700 pl-4 py-1 text-gray-700 dark:text-gray-300 italic my-4 text-sm"
          {...props}
        >
          {children}
        </blockquote>
      );
    },
    table({ children, ...props }: { children?: React.ReactNode }) {
      return (
        <div className="overflow-x-auto my-6 rounded-md">
          <table className="min-w-full text-sm border-collapse" {...props}>
            {children}
          </table>
        </div>
      );
    },
    thead({ children, ...props }: { children?: React.ReactNode }) {
      return <thead className="bg-gray-100 dark:bg-gray-800" {...props}>{children}</thead>;
    },
    tbody({ children, ...props }: { children?: React.ReactNode }) {
      return <tbody className="divide-y divide-gray-200 dark:divide-gray-700" {...props}>{children}</tbody>;
    },
    tr({ children, ...props }: { children?: React.ReactNode }) {
      return <tr className="hover:bg-gray-50 dark:hover:bg-gray-900" {...props}>{children}</tr>;
    },
    th({ children, ...props }: { children?: React.ReactNode }) {
      return (
        <th
          className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300"
          {...props}
        >
          {children}
        </th>
      );
    },
    td({ children, ...props }: { children?: React.ReactNode }) {
      return <td className="px-4 py-3 border-t border-gray-200 dark:border-gray-700" {...props}>{children}</td>;
    },
    code(props: {
      inline?: boolean;
      className?: string;
      children?: React.ReactNode;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      [key: string]: any; // Using any here as it's required for ReactMarkdown components
    }) {
      const { inline, className, children, ...otherProps } = props;
      const match = /language-(\w+)/.exec(className || '');
      const codeContent = children ? String(children).replace(/\n$/, '') : '';

      // Handle Mermaid diagrams
      if (!inline && match && match[1] === 'mermaid') {
        return (
          <div className="my-8 bg-gray-50 dark:bg-gray-800 rounded-md overflow-hidden shadow-sm">
            <Mermaid
              chart={codeContent}
              className="w-full max-w-full"
              zoomingEnabled={true}
            />
          </div>
        );
      }

      // Handle code blocks
      if (!inline && match) {
        return (
          <div className="my-6 rounded-md overflow-hidden text-sm shadow-sm">
            <div className="bg-gray-800 text-gray-200 px-5 py-2 text-sm flex justify-between items-center">
              <span>{match[1]}</span>
              <button
                onClick={() => {
                  navigator.clipboard.writeText(codeContent);
                }}
                className="text-gray-400 hover:text-white"
                title="Copy code"
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  className="h-5 w-5"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"
                  />
                </svg>
              </button>
            </div>
            <SyntaxHighlighter
              language={match[1]}
              style={tomorrow}
              className="!text-sm"
              customStyle={{ margin: 0, borderRadius: '0 0 0.375rem 0.375rem', padding: '1rem' }}
              showLineNumbers={true}
              wrapLines={true}
              wrapLongLines={true}
              {...otherProps}
            >
              {codeContent}
            </SyntaxHighlighter>
          </div>
        );
      }

      // Handle inline code
      return (
        <code
          className={`${className} font-mono bg-gray-100 dark:bg-gray-800 px-2 py-0.5 rounded text-pink-500 dark:text-pink-400 text-sm`}
          {...otherProps}
        >
          {children}
        </code>
      );
    },
  };

  return (
    <div className="prose prose-base dark:prose-invert max-w-none px-2 py-4">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeRaw, rehypeKatex]}
        components={MarkdownComponents}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
};

export default Markdown;