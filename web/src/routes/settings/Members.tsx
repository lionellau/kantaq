/**
 * E21-T1 (MOD-13) — Settings → Members over the MOD-06 API.
 *
 * Invite (with role; an Agent invite carries the documented default scopes),
 * list, revoke, rotate. A minted token appears exactly once — in the panel
 * right after the invite/rotate that created it (NFR-E06-1) — and is gone for
 * good once dismissed.
 */

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { Member } from "../../api/types";
import { fmtDateTime } from "../../lib/format";
import { useSession } from "../../lib/session";
import * as ui from "../../lib/ui";

const ROLES = ["Member", "Maintainer", "Viewer", "Agent"] as const;

// The propose-first default for agent tokens (docs/mcp.md): read tickets,
// store proposals — never a direct write.
const AGENT_SCOPES = ["tickets.read", "proposals.write"];

interface MintedToken {
  memberEmail: string;
  token: string;
}

export default function Members() {
  const { connected } = useSession();
  const [members, setMembers] = useState<Member[] | null>(null);
  const [minted, setMinted] = useState<MintedToken | null>(null);
  // Two independent error channels: loadError is owned by refresh() (the member
  // list fetch); actionError is owned by invite/revoke/rotate. Keeping them
  // apart stops a background list refresh — which clears loadError on success —
  // from wiping a still-relevant action error (e.g. the invite permission guard).
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const { data, error: apiError } = await api.GET("/v1/members");
    if (apiError !== undefined) {
      setLoadError("could not load members");
      return;
    }
    setLoadError(null);
    setMembers(data);
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function revoke(member: Member) {
    const { error: apiError, response } = await api.POST("/v1/members/{member_id}/revoke", {
      params: { path: { member_id: member.id } },
    });
    if (apiError !== undefined) {
      setActionError(
        response?.status === 409
          ? "cannot revoke the last owner"
          : `could not revoke ${member.email}`,
      );
      return;
    }
    setActionError(null);
    void refresh();
  }

  async function rotate(member: Member) {
    const { data, error: apiError } = await api.POST("/v1/members/{member_id}/rotate", {
      params: { path: { member_id: member.id } },
    });
    if (apiError !== undefined || data === undefined) {
      setActionError(`could not rotate the token for ${member.email}`);
      return;
    }
    setActionError(null);
    setMinted({ memberEmail: member.email, token: data.token });
    void refresh();
  }

  if (!connected) {
    return (
      <section>
        <h1>Members</h1>
        <p style={ui.muted}>
          Not connected. Paste your runtime token in <Link to="/settings">Settings</Link> first.
        </p>
      </section>
    );
  }

  return (
    <section>
      <p style={{ margin: 0 }}>
        <Link to="/settings" style={ui.muted}>
          ← Settings
        </Link>
      </p>
      <h1>Members</h1>

      {minted !== null && (
        <div role="alert" style={{ ...ui.card, borderColor: ui.palette.warnText }}>
          <p style={{ marginTop: 0 }}>
            Token for <strong>{minted.memberEmail}</strong> — shown once, store it now:
          </p>
          <code data-testid="minted-token" style={{ wordBreak: "break-all" }}>
            {minted.token}
          </code>
          <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
            <CopyButton text={minted.token} />
            <button type="button" style={ui.button} onClick={() => setMinted(null)}>
              Dismiss
            </button>
          </div>
        </div>
      )}

      {loadError !== null && <p style={ui.errorText}>{loadError}</p>}
      {actionError !== null && <p style={ui.errorText}>{actionError}</p>}

      <InviteForm
        onInvited={(email, token) => {
          setActionError(null);
          setMinted({ memberEmail: email, token });
          void refresh();
        }}
        onError={setActionError}
      />

      {members !== null && (
        <table style={ui.table}>
          <thead>
            <tr>
              <th style={ui.th}>Email</th>
              <th style={ui.th}>Role</th>
              <th style={ui.th}>Status</th>
              <th style={ui.th}>Joined</th>
              <th style={ui.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {members.map((member) => (
              <tr key={member.id}>
                <td style={ui.td}>{member.email}</td>
                <td style={ui.td}>{member.role}</td>
                <td style={ui.td}>{member.status}</td>
                <td style={ui.td}>{fmtDateTime(member.created_at)}</td>
                <td style={{ ...ui.td, whiteSpace: "nowrap" }}>
                  <button
                    type="button"
                    style={ui.button}
                    onClick={() => void rotate(member)}
                    disabled={member.status !== "active"}
                  >
                    Rotate token
                  </button>{" "}
                  <button
                    type="button"
                    style={ui.dangerButton}
                    onClick={() => void revoke(member)}
                    disabled={member.status !== "active"}
                  >
                    Revoke
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function InviteForm({
  onInvited,
  onError,
}: {
  onInvited: (email: string, token: string) => void;
  onError: (message: string) => void;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<(typeof ROLES)[number]>("Member");

  async function invite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!email.trim()) {
      return;
    }
    const {
      data,
      error: apiError,
      response,
    } = await api.POST("/v1/members/invite", {
      body: {
        email: email.trim(),
        role,
        scopes: role === "Agent" ? AGENT_SCOPES : [],
      },
    });
    if (apiError !== undefined || data === undefined) {
      onError(
        response?.status === 403
          ? "your role may not invite members"
          : "could not invite the member",
      );
      return;
    }
    setEmail("");
    onInvited(data.member.email, data.token);
  }

  return (
    <form
      aria-label="Invite a member"
      onSubmit={invite}
      style={{ display: "flex", gap: 8, alignItems: "end", margin: "1rem 0", flexWrap: "wrap" }}
    >
      <label style={ui.label}>
        Invite by email
        <input
          type="email"
          style={{ ...ui.input, minWidth: "16rem" }}
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          placeholder="teammate@example.com"
        />
      </label>
      <label style={ui.label}>
        Role
        <select
          style={ui.input}
          value={role}
          onChange={(event) => setRole(event.target.value as (typeof ROLES)[number])}
        >
          {ROLES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" style={ui.primaryButton}>
        Invite
      </button>
      {role === "Agent" && (
        <span style={ui.muted}>scopes: {AGENT_SCOPES.join(", ")} (propose-first)</span>
      )}
    </form>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // clipboard unavailable (insecure context): the token is selectable text
    }
  }

  return (
    <button type="button" style={ui.button} onClick={() => void copy()}>
      {copied ? "Copied" : "Copy"}
    </button>
  );
}
