import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ResearchFlow AI",
  description: "Agentic research automation with strict human-in-the-loop orchestration.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="dark">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        {/* Space Grotesk = distinctive display/headings; IBM Plex Sans = trustworthy,
            data-legible body (per .agents/skills typography "Financial Trust" pairing);
            JetBrains Mono = numeric/matrix/data cells. Deliberately NOT Inter — the
            frontend-design skill flags it as generic AI aesthetic. */}
        <link
          href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Space+Grotesk:wght@400;500;600;700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
