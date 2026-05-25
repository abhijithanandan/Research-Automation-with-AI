// Minimal, dependency-free Markdown renderer.
// Covers the subset the Critic's narrative summary uses: headings, bold,
// italic, inline code, unordered/ordered lists, and paragraphs. Intentionally
// small — a full markdown library is overkill for agent-generated prose.

import { Fragment, type ReactNode } from "react";

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  // Split on **bold**, *italic*, and `code` while keeping the delimiters.
  const pattern = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g;
  const parts = text.split(pattern);
  parts.forEach((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if (part.startsWith("**") && part.endsWith("**")) {
      nodes.push(
        <strong key={key} className="font-semibold text-slate-100">
          {part.slice(2, -2)}
        </strong>,
      );
    } else if (part.startsWith("*") && part.endsWith("*")) {
      nodes.push(
        <em key={key} className="italic text-slate-300">
          {part.slice(1, -1)}
        </em>,
      );
    } else if (part.startsWith("`") && part.endsWith("`")) {
      nodes.push(
        <code
          key={key}
          className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[0.85em] text-blue-300"
        >
          {part.slice(1, -1)}
        </code>,
      );
    } else if (part) {
      nodes.push(<Fragment key={key}>{part}</Fragment>);
    }
  });
  return nodes;
}

export function Markdown({ content }: { content: string }) {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];

  let listItems: { ordered: boolean; text: string }[] = [];
  let paragraph: string[] = [];

  function flushParagraph() {
    if (paragraph.length === 0) return;
    const text = paragraph.join(" ");
    blocks.push(
      <p key={`p-${blocks.length}`} className="text-sm leading-relaxed text-slate-400">
        {renderInline(text, `p-${blocks.length}`)}
      </p>,
    );
    paragraph = [];
  }

  function flushList() {
    const first = listItems[0];
    if (!first) return;
    const ordered = first.ordered;
    const items = listItems.map((it, i) => (
      <li key={i} className="text-sm leading-relaxed text-slate-400">
        {renderInline(it.text, `li-${blocks.length}-${i}`)}
      </li>
    ));
    blocks.push(
      ordered ? (
        <ol key={`ol-${blocks.length}`} className="ml-5 list-decimal space-y-1">
          {items}
        </ol>
      ) : (
        <ul key={`ul-${blocks.length}`} className="ml-5 list-disc space-y-1 marker:text-blue-500/60">
          {items}
        </ul>
      ),
    );
    listItems = [];
  }

  for (const raw of lines) {
    const line = raw.trimEnd();

    if (line.trim() === "") {
      flushParagraph();
      flushList();
      continue;
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      const level = (heading[1] ?? "").length;
      const text = heading[2] ?? "";
      const cls =
        level === 1
          ? "text-lg font-bold text-slate-100"
          : level === 2
            ? "text-base font-semibold text-slate-100"
            : "text-sm font-semibold uppercase tracking-wider text-slate-300";
      blocks.push(
        <p key={`h-${blocks.length}`} className={cls}>
          {renderInline(text, `h-${blocks.length}`)}
        </p>,
      );
      continue;
    }

    const ulItem = /^[-*]\s+(.*)$/.exec(line);
    if (ulItem) {
      flushParagraph();
      listItems.push({ ordered: false, text: ulItem[1] ?? "" });
      continue;
    }

    const olItem = /^\d+\.\s+(.*)$/.exec(line);
    if (olItem) {
      flushParagraph();
      listItems.push({ ordered: true, text: olItem[1] ?? "" });
      continue;
    }

    // Plain text — accumulate into the current paragraph.
    flushList();
    paragraph.push(line.trim());
  }

  flushParagraph();
  flushList();

  return <div className="space-y-3">{blocks}</div>;
}
