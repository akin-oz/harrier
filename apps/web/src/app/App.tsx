import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import type { Job } from "../entities/job";
import { ApplyPage } from "../pages/apply/ApplyPage";
import { TrackerPage } from "../pages/tracker/TrackerPage";
import "../shared/ui/tokens.css";
import { Header } from "../widgets/header/Header";
import "./App.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: false },
  },
});

export function App() {
  // Which page is showing lives here rather than in either page: a page that
  // rendered another page would cross the layer direction FSD keeps one way
  // (ADR-001, and the fsd-reviewer enforces it). There is no router yet, and
  // one job at a time is all spec 047's Apply page needs.
  const [applying, setApplying] = useState<Job | null>(null);

  return (
    <QueryClientProvider client={queryClient}>
      <div className="app-shell">
        <Header />
        <main className="app-main">
          {applying === null ? (
            <TrackerPage onApply={setApplying} />
          ) : (
            <ApplyPage
              job={applying}
              onBack={() => {
                setApplying(null);
              }}
            />
          )}
        </main>
      </div>
    </QueryClientProvider>
  );
}
