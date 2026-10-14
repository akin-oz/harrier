import { useId, useState } from "react";
import type { SyntheticEvent } from "react";

import { requestCode, verifyCode } from "../../shared/auth/session";
import "./auth.css";

// What the page says, never more than this about an address (spec 114): it
// does not say whether the address has an account.
export const CODE_SENT = "If this address has an account, a sign-in code is on its way.";
export const RATE_LIMITED = "Too many requests. Wait before asking for another code.";
export const UNREACHABLE = "Could not reach the sign-in service.";
export const WRONG_CODE = "That code is wrong or has expired.";

/**
 * The two steps of an emailed one-time code: the address, then the code.
 *
 * The address is personal data. It lives in this component's state and goes
 * only to `requestCode` and `verifyCode`: never to a URL, to storage written
 * from here, to a log, or into an error message. A verified code fires
 * `SIGNED_IN`, and the gate above this renders the app.
 */
export function SignInForm() {
  const emailId = useId();
  const codeId = useId();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<"email" | "code">("email");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function ask(event: SyntheticEvent): Promise<void> {
    event.preventDefault();
    setBusy(true);
    const answer = await requestCode(email.trim());
    setBusy(false);
    if (answer === "sent") {
      setStep("code");
      setMessage(CODE_SENT);
    } else {
      setMessage(answer === "rate-limited" ? RATE_LIMITED : UNREACHABLE);
    }
  }

  async function check(event: SyntheticEvent): Promise<void> {
    event.preventDefault();
    setBusy(true);
    const answer = await verifyCode(email.trim(), code.trim());
    setBusy(false);
    // On success the session event replaces this form; nothing to show.
    if (answer === "refused") setMessage(WRONG_CODE);
    if (answer === "unreachable") setMessage(UNREACHABLE);
  }

  return (
    <form
      className="sign-in-form"
      onSubmit={(event) => {
        void (step === "email" ? ask(event) : check(event));
      }}
    >
      {step === "email" ? (
        <>
          <label htmlFor={emailId} className="sign-in-form__label">
            Email address
          </label>
          <input
            id={emailId}
            type="email"
            name="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => {
              setEmail(event.target.value);
            }}
          />
          <button type="submit" disabled={busy}>
            Send a sign-in code
          </button>
        </>
      ) : (
        <>
          <label htmlFor={codeId} className="sign-in-form__label">
            Sign-in code
          </label>
          <input
            id={codeId}
            type="text"
            name="code"
            inputMode="numeric"
            autoComplete="one-time-code"
            required
            value={code}
            onChange={(event) => {
              setCode(event.target.value);
            }}
          />
          <button type="submit" disabled={busy}>
            Sign in
          </button>
          <button
            type="button"
            className="sign-in-form__again"
            disabled={busy}
            onClick={() => {
              setStep("email");
              setCode("");
              setMessage(null);
            }}
          >
            Ask for a new code
          </button>
        </>
      )}
      {message !== null && (
        <p role="status" className="sign-in-form__message">
          {message}
        </p>
      )}
    </form>
  );
}
