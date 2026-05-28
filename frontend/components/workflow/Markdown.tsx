"use client";

// Markdown renderer for Critic-produced narrative + matrix tables.
// Uses react-markdown + remark-gfm so GitHub-flavored tables render as real
// HTML tables (the previous dependency-free parser silently collapsed every
// table row into a single mushed paragraph — see audit fix 2026-05-26).

import type { ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

// react-markdown's Components type loosely-types element overrides — TS
// `strict: noImplicitAny` won't infer the destructured props. Capture the
// few props each handler reads in a single named type so each handler stays
// readable and the file passes `tsc --noEmit`.
type MdProps = {
  children?: ReactNode;
  className?: string;
  href?: string;
};

// Two reading scales:
//   "compact"  — the in-flow narrative tab; tight, sits inside a review card.
//   "prose"    — the long-form manuscript reading experience; larger type,
//                generous line-height, bright tracking-tight headings, muted
//                off-white body to cut eye strain on long sessions.
type MdVariant = "compact" | "prose";

function buildComponents(variant: MdVariant): Components {
  const prose = variant === "prose";
  return {
    // Headings: stark bright-white, tracking-tight (mandate §3 hierarchy).
    h1: ({ children }: MdProps) => (
      <h1
        className={cn(
          "font-display font-extrabold tracking-tight text-foreground",
          prose ? "mt-10 mb-4 text-3xl first:mt-0" : "mt-4 mb-2 text-lg",
        )}
      >
        {children}
      </h1>
    ),
    h2: ({ children }: MdProps) => (
      <h2
        className={cn(
          "font-display font-bold tracking-tight text-foreground",
          prose ? "mt-9 mb-3 text-2xl" : "mt-4 mb-2 text-base",
        )}
      >
        {children}
      </h2>
    ),
    h3: ({ children }: MdProps) => (
      <h3
        className={cn(
          "font-display font-semibold tracking-tight text-foreground",
          prose ? "mt-7 mb-2 text-lg" : "mt-3 mb-1.5 text-sm uppercase tracking-wider",
        )}
      >
        {children}
      </h3>
    ),
    h4: ({ children }: MdProps) => (
      <h4
        className={cn(
          "font-semibold uppercase tracking-wider text-muted",
          prose ? "mt-5 mb-1.5 text-xs" : "mt-2 mb-1 text-xs",
        )}
      >
        {children}
      </h4>
    ),
    // Body: muted off-white, comfortable leading. Prose gets a larger size +
    // looser leading so long passages read like a paper, not a tooltip.
    p: ({ children }: MdProps) => (
      <p
        className={cn(
          "text-muted last:mb-0",
          prose ? "mb-5 text-[15px] leading-[1.75]" : "mb-3 text-sm leading-relaxed",
        )}
      >
        {children}
      </p>
    ),
    strong: ({ children }: MdProps) => (
      <strong className="font-semibold text-foreground">{children}</strong>
    ),
    em: ({ children }: MdProps) => <em className="italic text-foreground">{children}</em>,
    code: ({ children, className }: MdProps) => {
      // Block code falls through to <pre>; only style the inline form here.
      if (className) return <code className={className}>{children}</code>;
      return (
        <code className="rounded bg-surface-elevated px-1.5 py-0.5 font-mono text-[0.85em] text-primary">
          {children}
        </code>
      );
    },
    pre: ({ children }: MdProps) => (
      <pre
        className={cn(
          "overflow-x-auto rounded-lg bg-surface-elevated p-4 font-mono text-xs leading-relaxed text-foreground",
          prose ? "mb-5" : "mb-3",
        )}
      >
        {children}
      </pre>
    ),
    ul: ({ children }: MdProps) => (
      <ul
        className={cn(
          "ml-5 list-disc text-muted marker:text-primary/60",
          prose ? "mb-5 space-y-2 text-[15px] leading-[1.7]" : "mb-3 space-y-1 text-sm",
        )}
      >
        {children}
      </ul>
    ),
    ol: ({ children }: MdProps) => (
      <ol
        className={cn(
          "ml-5 list-decimal text-muted",
          prose ? "mb-5 space-y-2 text-[15px] leading-[1.7]" : "mb-3 space-y-1 text-sm",
        )}
      >
        {children}
      </ol>
    ),
    li: ({ children }: MdProps) => <li className="leading-relaxed">{children}</li>,
    a: ({ children, href }: MdProps) => (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="text-primary underline decoration-primary/40 underline-offset-2 transition-colors duration-150 hover:decoration-primary"
      >
        {children}
      </a>
    ),
    blockquote: ({ children }: MdProps) => (
      <blockquote
        className={cn(
          "border-l-2 border-primary/40 pl-4 italic text-foreground",
          prose ? "my-5 text-[15px] leading-[1.7]" : "mb-3 py-2 text-sm",
        )}
      >
        {children}
      </blockquote>
    ),
    hr: () => <hr className={cn("border-border/60", prose ? "my-8" : "my-4")} />,
    // GitHub-flavored tables — borderless, hairline-divided, matches MatrixTable.
    table: ({ children }: MdProps) => (
      <div className={cn("overflow-x-auto", prose ? "mb-5" : "mb-3")}>
        <table className="w-full border-collapse text-xs">{children}</table>
      </div>
    ),
    thead: ({ children }: MdProps) => <thead className="bg-surface-elevated">{children}</thead>,
    tbody: ({ children }: MdProps) => (
      <tbody className="divide-y divide-border">{children}</tbody>
    ),
    tr: ({ children }: MdProps) => (
      <tr className="transition-colors duration-150 ease-in-out hover:bg-primary/[0.04]">
        {children}
      </tr>
    ),
    th: ({ children }: MdProps) => (
      <th className="px-3 py-2.5 text-left font-mono text-[10px] font-semibold uppercase tracking-[0.15em] text-muted-foreground">
        {children}
      </th>
    ),
    td: ({ children }: MdProps) => (
      <td className="px-3 py-2.5 align-top leading-relaxed text-muted">{children}</td>
    ),
  };
}

const COMPACT_COMPONENTS = buildComponents("compact");
const PROSE_COMPONENTS = buildComponents("prose");

export function Markdown({
  content,
  variant = "compact",
}: {
  content: string;
  variant?: MdVariant;
}) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={variant === "prose" ? PROSE_COMPONENTS : COMPACT_COMPONENTS}
    >
      {content}
    </ReactMarkdown>
  );
}
