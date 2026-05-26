"use client";

// Markdown renderer for Critic-produced narrative + matrix tables.
// Uses react-markdown + remark-gfm so GitHub-flavored tables render as real
// HTML tables (the previous dependency-free parser silently collapsed every
// table row into a single mushed paragraph — see audit fix 2026-05-26).

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// Component overrides scope styling to the ResearchFlow dark palette
// (slate-200 body, blue accent) so we don't ship a global prose stylesheet
// just for one component.
const COMPONENTS: Components = {
  h1: ({ children }) => (
    <h1 className="mt-4 mb-2 text-lg font-bold text-slate-100">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="mt-4 mb-2 text-base font-semibold text-slate-100">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="mt-3 mb-1.5 text-sm font-semibold uppercase tracking-wider text-slate-300">
      {children}
    </h3>
  ),
  h4: ({ children }) => (
    <h4 className="mt-2 mb-1 text-xs font-semibold uppercase tracking-wider text-slate-400">
      {children}
    </h4>
  ),
  p: ({ children }) => (
    <p className="mb-3 text-sm leading-relaxed text-slate-400 last:mb-0">{children}</p>
  ),
  strong: ({ children }) => (
    <strong className="font-semibold text-slate-100">{children}</strong>
  ),
  em: ({ children }) => <em className="italic text-slate-300">{children}</em>,
  code: ({ children, className }) => {
    // Block code: react-markdown emits <pre><code class="language-x">. Inline
    // code has no className. Style only the inline form here; block code falls
    // through to the <pre> handler below.
    if (className) {
      return <code className={className}>{children}</code>;
    }
    return (
      <code className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[0.85em] text-blue-300">
        {children}
      </code>
    );
  },
  pre: ({ children }) => (
    <pre className="mb-3 overflow-x-auto rounded-lg border border-slate-700 bg-[#0a0f1e] p-3 font-mono text-xs leading-relaxed text-slate-300">
      {children}
    </pre>
  ),
  ul: ({ children }) => (
    <ul className="mb-3 ml-5 list-disc space-y-1 text-sm text-slate-400 marker:text-blue-500/60">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="mb-3 ml-5 list-decimal space-y-1 text-sm text-slate-400">{children}</ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-blue-400 underline decoration-blue-500/40 underline-offset-2 transition-colors hover:text-blue-300 hover:decoration-blue-400"
    >
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="mb-3 border-l-2 border-blue-500/40 bg-blue-500/5 py-2 pl-3 text-sm italic text-slate-300">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-4 border-slate-700/60" />,
  // GitHub-flavored tables — the entire reason this component exists.
  // Tailwind utility classes give us a scrollable, readable grid that matches
  // the MatrixTable aesthetic without a global table stylesheet.
  table: ({ children }) => (
    <div className="mb-3 overflow-x-auto rounded-lg border border-[#1e2d45]">
      <table className="w-full border-collapse text-xs">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-[#0a0f1e]">{children}</thead>,
  tbody: ({ children }) => <tbody>{children}</tbody>,
  tr: ({ children }) => (
    <tr className="border-b border-[#1a2236] last:border-b-0 hover:bg-[#1a2236]">
      {children}
    </tr>
  ),
  th: ({ children }) => (
    <th className="border-b border-[#1e2d45] px-3 py-2.5 text-left font-semibold uppercase tracking-wider text-slate-400">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="px-3 py-2.5 align-top leading-relaxed text-slate-400">{children}</td>
  ),
};

export function Markdown({ content }: { content: string }) {
  return (
    <div className="space-y-1">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
