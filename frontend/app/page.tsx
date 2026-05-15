export default function HomePage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-16">
      <header className="space-y-2">
        <p className="text-sm font-medium uppercase tracking-wider text-slate-500">
          ResearchFlow AI
        </p>
        <h1 className="text-4xl font-semibold">
          Agentic research automation with humans in the loop.
        </h1>
        <p className="text-slate-600 dark:text-slate-300">
          This is the boilerplate landing page. Implementation begins after the team has read{" "}
          <code className="rounded bg-slate-100 px-1 py-0.5 text-sm dark:bg-slate-800">
            BRD.md
          </code>{" "}
          and{" "}
          <code className="rounded bg-slate-100 px-1 py-0.5 text-sm dark:bg-slate-800">
            SPEC.md
          </code>
          .
        </p>
      </header>

      <section className="rounded-lg border border-slate-200 p-6 dark:border-slate-800">
        <h2 className="text-lg font-semibold">Next steps for the team</h2>
        <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm text-slate-700 dark:text-slate-300">
          <li>Run <code>docker compose up</code> from the repo root.</li>
          <li>Open <code>/api/v1/health</code> on the backend.</li>
          <li>Implement <code>/projects</code> route and the project-create flow.</li>
          <li>Wire the WS event handler (see <code>lib/ws.ts</code>).</li>
          <li>Build the approval panel UI per <code>SPEC.md §7</code>.</li>
        </ol>
      </section>
    </main>
  );
}
