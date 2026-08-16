import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import type { Job } from "../entities/job";
import { ApplyPage } from "../pages/apply/ApplyPage";
import { OutreachPage } from "../pages/outreach/OutreachPage";
import { TrackerPage } from "../pages/tracker/TrackerPage";
import "../shared/ui/tokens.css";
import { Header } from "../widgets/header/Header";
import "./App.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: false },
  },
});

const SECTIONS = [
  { id: "tracker", label: "Tracker" },
  { id: "outreach", label: "Outreach" },
] as const;

type Section = (typeof SECTIONS)[number]["id"];

export function App() {
  // Which page is showing lives here rather than in any page: a page that
  // rendered another page would cross the layer direction FSD keeps one way
  // (ADR-001, and the fsd-reviewer enforces it). There is still no router;
  // two sections and a per-job Apply view is all specs 047 and 048 need.
  const [section, setSection] = useState<Section>("tracker");
  const [applying, setApplying] = useState<Job | null>(null);

  return (
    <QueryClientProvider client={queryClient}>
      <div className="app-shell">
        <Header />
        <nav className="app-nav" aria-label="Sections">
          {SECTIONS.map((entry) => (
            <button
              key={entry.id}
              type="button"
              className={`app-nav__link${section === entry.id && applying === null ? " app-nav__link--active" : ""}`}
              aria-current={section === entry.id && applying === null ? "page" : undefined}
              onClick={() => {
                setApplying(null);
                setSection(entry.id);
              }}
            >
              {entry.label}
            </button>
          ))}
        </nav>
        <main className="app-main">
          {applying !== null ? (
            <ApplyPage
              job={applying}
              onBack={() => {
                setApplying(null);
              }}
            />
          ) : section === "tracker" ? (
            <TrackerPage onApply={setApplying} />
          ) : (
            <OutreachPage />
          )}
        </main>
      </div>
    </QueryClientProvider>
  );
}
