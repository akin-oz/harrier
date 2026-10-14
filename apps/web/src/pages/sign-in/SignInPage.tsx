import { SignInForm } from "../../features/auth/SignInForm";
import type { SessionNotice } from "../../shared/auth/session";
import "./SignInPage.css";

// One sentence for a session that ended under the person (spec 114).
const NOTICE: Record<Exclude<SessionNotice, null>, string> = {
  expired: "Your session ended. Sign in again.",
  invalid: "Your session ended. Sign in again.",
  refused: "This account cannot use harrier.",
};

/** What a person sees with no session: the sign-in form and nothing else. */
export function SignInPage({ notice }: { notice: SessionNotice }) {
  return (
    <main className="sign-in-page">
      <h1 className="sign-in-page__title">Harrier</h1>
      {notice !== null && (
        <p role="alert" className="sign-in-page__notice">
          {NOTICE[notice]}
        </p>
      )}
      <SignInForm />
    </main>
  );
}
