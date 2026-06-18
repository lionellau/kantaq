/**
 * TokenShowHint (DEBT-34): the literal `kantaq token show` command with a Copy
 * button. The persona study found the disconnected Backlog told users to "paste
 * your token in Settings" but withheld *how* to get it — the command only
 * appeared once you reached Settings. This surfaces it where the user is blocked.
 */

import { useState } from "react";
import * as ui from "../lib/ui";

const COMMAND = "kantaq token show";

export default function TokenShowHint() {
  const [copied, setCopied] = useState(false);

  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(COMMAND);
      setCopied(true);
    } catch {
      // clipboard unavailable: the command is selectable text
    }
  }

  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <code style={{ ...ui.chip, fontFamily: "monospace", padding: "0.25rem 0.5rem" }}>
        {COMMAND}
      </code>
      <button type="button" style={ui.button} onClick={() => void copy()}>
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
