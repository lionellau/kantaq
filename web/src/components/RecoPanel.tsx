/**
 * E17-T2 (MOD-22) — the ticket recommendation panel.
 *
 * Renders the structured recommendation contract the runtime returns for a
 * ticket (role, skill container, why, required/missing memory, expected output,
 * mapped tool, risk, confidence, approval rule) and a one-click "Copy MCP
 * snippet" that puts the ready-to-paste session template on the clipboard.
 *
 * Recommendations are system-generated (keyed on the lifecycle stage + label
 * signals, MOD-22) — no user-authored markdown — so everything renders as plain
 * text. The full right rail that hosts this panel is E19-T4; here it ships behind
 * the `VITE_RECO_PANEL` flag (see lib/flags).
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { Recommendation } from "../api/types";
import * as ui from "../lib/ui";

const CONFIDENCE_LABEL: Record<string, string> = {
  rule_match_strong: "strong match",
  rule_match_partial: "label match",
  heuristic_only: "fallback",
};

const RISK_COLOR: Record<string, { bg: string; text: string }> = {
  high: { bg: "#fde2e1", text: ui.palette.danger },
  medium: { bg: ui.palette.warnBg, text: ui.palette.warnText },
  low: { bg: ui.palette.surface, text: ui.palette.muted },
};

export default function RecoPanel({ ticketId }: { ticketId: string }) {
  const [recs, setRecs] = useState<Recommendation[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let live = true;
    void api
      .GET("/v1/tickets/{ticket_id}/recommendations", {
        params: { path: { ticket_id: ticketId } },
      })
      .then(({ data, error: apiError }) => {
        if (!live) return;
        if (apiError !== undefined) {
          setError(true);
          return;
        }
        setRecs(data ?? []);
      });
    return () => {
      live = false;
    };
  }, [ticketId]);

  return (
    <section aria-labelledby="reco-heading">
      <h2 id="reco-heading" style={{ ...ui.sectionHeading, marginTop: 0 }}>
        Recommended roles &amp; skills
      </h2>
      {error && <p style={ui.errorText}>Could not load recommendations.</p>}
      {!error && recs === null && <p style={ui.muted}>Loading…</p>}
      {!error && recs !== null && recs.length === 0 && (
        <p style={ui.muted}>No recommendations for this stage.</p>
      )}
      <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 12 }}>
        {(recs ?? []).map((rec) => (
          <RecoCard key={`${rec.skill_container}:${rec.role}`} rec={rec} />
        ))}
      </ul>
    </section>
  );
}

function RecoCard({ rec }: { rec: Recommendation }) {
  const risk = RISK_COLOR[rec.risk_level] ?? RISK_COLOR.low;
  return (
    <li style={{ ...ui.card, padding: "0.75rem" }}>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "baseline" }}>
        <span
          style={{
            ...ui.chip,
            background: ui.palette.accent,
            color: "white",
            borderColor: ui.palette.accent,
          }}
        >
          {rec.role}
        </span>
        <span style={{ fontWeight: 600 }}>{skillName(rec.skill_container)}</span>
      </div>

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 6 }}>
        <span style={ui.chip}>{CONFIDENCE_LABEL[rec.confidence] ?? rec.confidence}</span>
        <span style={{ ...ui.chip, background: risk.bg, color: risk.text, borderColor: risk.bg }}>
          {rec.risk_level} risk
        </span>
        <span style={ui.chip}>
          {rec.approval_rule === "propose_first" ? "propose-first" : "read-only"}
        </span>
      </div>

      <p style={{ ...ui.muted, margin: "8px 0 0" }}>{rec.why}</p>

      <p style={{ margin: "8px 0 0", fontSize: "0.8125rem" }}>
        <span style={{ ...ui.label, display: "block", marginBottom: 2 }}>Expected output</span>
        {rec.expected_output}
      </p>

      {rec.missing_memory.length > 0 && (
        <p style={{ margin: "8px 0 0", fontSize: "0.75rem", color: ui.palette.warnText }}>
          Missing context: {rec.missing_memory.join(", ")}
        </p>
      )}

      <p style={{ ...ui.muted, margin: "6px 0 0", fontSize: "0.75rem" }}>
        Run with: {rec.mapped_tool}
      </p>

      <CopySnippetButton snippet={rec.mcp_session_template} />
    </li>
  );
}

function CopySnippetButton({ snippet }: { snippet: string }) {
  const [copied, setCopied] = useState(false);
  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(snippet);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }, [snippet]);

  return (
    <button
      type="button"
      style={{ ...ui.button, marginTop: 8, fontSize: "0.8125rem" }}
      onClick={() => void copy()}
    >
      {copied ? "Copied ✓" : "Copy MCP snippet"}
    </button>
  );
}

/** "code-review" -> "Code review" for display (the slug is the contract value). */
function skillName(slug: string): string {
  const spaced = slug.replace(/-/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
