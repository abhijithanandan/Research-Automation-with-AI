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
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
