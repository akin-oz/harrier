import { endSession } from "../../shared/auth/session";
import "./auth.css";

/**
 * Ends this browser's session (spec 114, Open decision 4). The gate then
 * unmounts the app, which aborts its open streams, empties the query cache
 * and shows the sign-in page.
 */
export function SignOutButton() {
  return (
    <button
      type="button"
      className="sign-out-button"
      onClick={() => {
        void endSession("signed-out");
      }}
    >
      Sign out
    </button>
  );
}
