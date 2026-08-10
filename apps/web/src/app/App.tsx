import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

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
  return (
    <QueryClientProvider client={queryClient}>
      <div className="app-shell">
        <Header />
        <main className="app-main">
          <TrackerPage />
        </main>
      </div>
    </QueryClientProvider>
  );
}
