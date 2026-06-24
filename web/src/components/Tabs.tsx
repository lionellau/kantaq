/**
 * E20-T4 (MOD-12) — a minimal, accessible tab strip for the Inbox.
 *
 * Framework-free (RISK-08): one `role="tablist"` of `role="tab"` buttons over a
 * `role="tabpanel"`. Each tab can carry a count badge (pending proposals,
 * denied calls). Kept tiny and controlled — the parent owns the active id — so
 * the Inbox stays the single source of which queue is showing.
 */

import type { CSSProperties, ReactNode } from "react";
import * as ui from "../lib/ui";

export interface TabDef {
  id: string;
  label: string;
  count?: number;
}

const tabBase: CSSProperties = {
  border: "none",
  background: "none",
  padding: "0.5rem 0.25rem",
  marginRight: "1.25rem",
  fontSize: "0.9375rem",
  fontWeight: 600,
  color: ui.palette.muted,
  cursor: "pointer",
  borderBottom: "2px solid transparent",
};

export default function Tabs({
  tabs,
  active,
  onSelect,
  children,
}: {
  tabs: TabDef[];
  active: string;
  onSelect: (id: string) => void;
  children: ReactNode;
}) {
  return (
    <div>
      <div
        role="tablist"
        style={{ display: "flex", borderBottom: `1px solid ${ui.palette.border}` }}
      >
        {tabs.map((tab) => {
          const selected = tab.id === active;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              id={`tab-${tab.id}`}
              aria-selected={selected}
              aria-controls={`tabpanel-${active}`}
              onClick={() => onSelect(tab.id)}
              style={{
                ...tabBase,
                color: selected ? ui.palette.text : ui.palette.muted,
                borderBottomColor: selected ? ui.palette.accent : "transparent",
              }}
            >
              {tab.label}
              {tab.count !== undefined && tab.count > 0 && (
                <span
                  aria-label={`${tab.count}`}
                  style={{
                    marginLeft: 6,
                    padding: "0.05rem 0.4rem",
                    borderRadius: ui.radius.pill,
                    fontSize: ui.text.xs,
                    background: ui.palette.surface,
                    border: `1px solid ${ui.palette.border}`,
                    color: ui.palette.muted,
                  }}
                >
                  {tab.count}
                </span>
              )}
            </button>
          );
        })}
      </div>
      <div
        role="tabpanel"
        id={`tabpanel-${active}`}
        aria-labelledby={`tab-${active}`}
        style={{ marginTop: "1rem" }}
      >
        {children}
      </div>
    </div>
  );
}
