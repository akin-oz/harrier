import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { TrackerPage } from "../pages/tracker/TrackerPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: false },
  },
});

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <main>
        <h1>Harrier</h1>
        <TrackerPage />
      </main>
    </QueryClientProvider>
  );
}
