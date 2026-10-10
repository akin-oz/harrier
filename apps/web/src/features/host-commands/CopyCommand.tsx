import { useEffect, useState } from "react";

import "./hostCommands.css";

type Copied = "idle" | "copied" | "failed";

/**
 * A command to run on the host, and a control that copies it (spec 096).
 *
 * The result is announced in a live region beside the button, so a screen
 * reader hears that the copy happened rather than only seeing a button. A
 * browser that refuses the clipboard is told so, and the command stays
 * selectable text either way.
 */
export function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState<Copied>("idle");

  useEffect(() => {
    if (copied === "idle") return undefined;
    const timer = setTimeout(() => {
      setCopied("idle");
    }, 4000);
    return () => {
      clearTimeout(timer);
    };
  }, [copied]);

  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(command);
      setCopied("copied");
    } catch {
      setCopied("failed");
    }
  }

  return (
    <div className="host-command">
      <code className="host-command__text">{command}</code>
      <button
        type="button"
        className="host-command__copy"
        aria-label={`Copy ${command}`}
        onClick={() => {
          void copy();
        }}
      >
        Copy
      </button>
      <span role="status" className="host-command__status">
        {copied === "copied" && "Copied"}
        {copied === "failed" && "Could not copy. Select the command and copy it."}
      </span>
    </div>
  );
}
