import { useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useRef } from "react";
import type { ReactNode } from "react";

import { SignInPage } from "../pages/sign-in/SignInPage";
import { useSession } from "../shared/auth/session";
import { appMode, configError } from "../shared/config/appMode";

/**
 * Who may see the app (spec 114).
 *
 * A build whose configuration was refused says so and renders nothing else:
 * no request is made. Local, the app is the app of spec 035 and this is a
 * pass-through that touches no session. Hosted, with no session, only the
 * sign-in page renders: no header, no health query, no track query.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const error = configError();
  if (error !== null) {
    return (
      <p role="alert" className="app-notice app-notice--error">
        This build of harrier is misconfigured: {error.message}
      </p>
    );
  }
  if (appMode().kind === "local") return <>{children}</>;
  return <HostedGate>{children}</HostedGate>;
}

function HostedGate({ children }: { children: ReactNode }) {
  const session = useSession();
  const queryClient = useQueryClient();
  // Whose rows the cache holds. The cache is keyed by track, not by owner
  // (spec 094), so without this a second person on the same browser would see
  // the first person's cached rows until each query refetched.
  const cacheOwner = useRef<string | null>(null);

  if (session.status === "signed-in") {
    // In render, so it happens before the app below renders: another tab
    // signing in as someone else changes the user under this one.
    if (cacheOwner.current !== null && cacheOwner.current !== session.userId) {
      queryClient.clear();
    }
    cacheOwner.current = session.userId;
  }

  useEffect(() => {
    // After the app has unmounted, and its streams aborted with it: nothing
    // of the signed-out person stays in memory.
    if (session.status === "signed-out") {
      queryClient.clear();
      cacheOwner.current = null;
    }
  }, [session.status, queryClient]);

  if (session.status === "loading") {
    return <p className="app-notice">Checking your session.</p>;
  }
  if (session.status === "signed-out") return <SignInPage notice={session.notice} />;
  // Keyed by the user, so a different person never inherits the old one's
  // component state.
  return <Fragment key={session.userId}>{children}</Fragment>;
}
