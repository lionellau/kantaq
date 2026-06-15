/**
 * E17-T5 (MOD-22) — Settings → Skill mappings: bind a skill container to the
 * tool you actually drive it with.
 *
 * The recommendation panel names a skill container and a default tool hint; a
 * mapping here replaces that hint with your own — "Code review → My Claude
 * Code" — so the recommendation reflects your setup. The registry is local and
 * off the sync surface; a mapping is a descriptive label, never an executable
 * binding and never a secret (DEBT-06/07). Any member may manage their own
 * mappings (skills.manage); a Viewer can read but not change them.
 */

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { SKILL_MAPPING_SCOPES, type SkillContainer, type SkillMapping } from "../../api/types";
import { useSession } from "../../lib/session";
import * as ui from "../../lib/ui";

export default function SkillMappings() {
  const { connected } = useSession();
  const [containers, setContainers] = useState<SkillContainer[] | null>(null);
  const [mappings, setMappings] = useState<SkillMapping[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [containerId, setContainerId] = useState("");
  const [scope, setScope] = useState<(typeof SKILL_MAPPING_SCOPES)[number]>("personal");
  const [connection, setConnection] = useState("");

  const refresh = useCallback(async () => {
    if (!connected) {
      return;
    }
    const [containersResult, mappingsResult] = await Promise.all([
      api.GET("/v1/skill-containers"),
      api.GET("/v1/skill-mappings"),
    ]);
    if (containersResult.error !== undefined || mappingsResult.error !== undefined) {
      setError("could not load the skill registry");
      return;
    }
    setError(null);
    setContainers(containersResult.data);
    setMappings(mappingsResult.data);
  }, [connected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!containerId) {
      return;
    }
    const { error: apiError, response } = await api.POST("/v1/skill-mappings", {
      body: { container_id: containerId, scope, provider: "", connection, status: "active" },
    });
    if (apiError !== undefined) {
      setError(
        response?.status === 403
          ? "you do not have permission to manage skill mappings"
          : "could not save the mapping",
      );
      return;
    }
    setContainerId("");
    setConnection("");
    void refresh();
  }

  async function toggle(mapping: SkillMapping) {
    const next = mapping.status === "active" ? "disabled" : "active";
    const { error: apiError } = await api.PATCH("/v1/skill-mappings/{mapping_id}", {
      params: { path: { mapping_id: mapping.id } },
      body: { status: next },
    });
    if (apiError !== undefined) {
      setError("could not update the mapping");
      return;
    }
    void refresh();
  }

  async function remove(mapping: SkillMapping) {
    const { error: apiError } = await api.DELETE("/v1/skill-mappings/{mapping_id}", {
      params: { path: { mapping_id: mapping.id } },
    });
    if (apiError !== undefined) {
      setError("could not delete the mapping");
      return;
    }
    void refresh();
  }

  if (!connected) {
    return (
      <section>
        <h1>Skill mappings</h1>
        <p style={ui.muted}>
          Not connected. Paste your runtime token in <Link to="/settings">Settings</Link> first.
        </p>
      </section>
    );
  }

  const nameFor = (id: string) => containers?.find((c) => c.id === id)?.name ?? id;

  return (
    <section>
      <p style={{ margin: 0 }}>
        <Link to="/settings" style={ui.muted}>
          ← Settings
        </Link>
      </p>
      <h1>Skill mappings</h1>
      <p style={ui.muted}>
        Map a skill container to the tool you drive it with. The recommendation panel shows your
        mapped tool instead of the generic hint. Labels are descriptive — kantaq never stores a
        secret or runs the tool for you.
      </p>

      {error !== null && <p style={ui.errorText}>{error}</p>}

      <form
        onSubmit={create}
        style={{ display: "flex", gap: 8, alignItems: "end", flexWrap: "wrap" }}
      >
        <label style={ui.label}>
          Skill container
          <select
            style={ui.input}
            value={containerId}
            onChange={(event) => setContainerId(event.target.value)}
            aria-label="Skill container"
          >
            <option value="">Select a container…</option>
            {(containers ?? []).map((container) => (
              <option key={container.id} value={container.id}>
                {container.name}
              </option>
            ))}
          </select>
        </label>
        <label style={ui.label}>
          Scope
          <select
            style={ui.input}
            value={scope}
            onChange={(event) =>
              setScope(event.target.value as (typeof SKILL_MAPPING_SCOPES)[number])
            }
            aria-label="Scope"
          >
            {SKILL_MAPPING_SCOPES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label style={ui.label}>
          Tool (description)
          <input
            type="text"
            style={ui.input}
            value={connection}
            onChange={(event) => setConnection(event.target.value)}
            placeholder="e.g. My Claude Code"
          />
        </label>
        <button type="submit" style={ui.primaryButton} disabled={!containerId}>
          Add mapping
        </button>
      </form>

      {mappings !== null &&
        (mappings.length === 0 ? (
          <p style={ui.muted}>No mappings yet.</p>
        ) : (
          <table style={ui.table}>
            <thead>
              <tr>
                <th style={ui.th}>Skill container</th>
                <th style={ui.th}>Tool</th>
                <th style={ui.th}>Scope</th>
                <th style={ui.th}>Status</th>
                <th style={ui.th}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {mappings.map((mapping) => (
                <tr key={mapping.id}>
                  <td style={ui.td}>{nameFor(mapping.container_id)}</td>
                  <td style={ui.td}>{mapping.connection || mapping.provider || "—"}</td>
                  <td style={ui.td}>{mapping.scope}</td>
                  <td style={ui.td}>
                    <span style={ui.chip}>{mapping.status}</span>
                  </td>
                  <td style={ui.td}>
                    <button type="button" style={ui.button} onClick={() => void toggle(mapping)}>
                      {mapping.status === "active" ? "Disable" : "Enable"}
                    </button>{" "}
                    <button
                      type="button"
                      style={ui.dangerButton}
                      onClick={() => void remove(mapping)}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </section>
  );
}
