/**
 * E19-T2 (MOD-11) — the ticket page.
 *
 * Three regions (FR-E19-3): the header (title + field chips), the body
 * (markdown description, the combined comments-and-activity timeline, the
 * attachments), and the right rail (sync badge, pending proposals, and — behind
 * the VITE_RECO_PANEL flag — the E17-T2 role/skill recommendation panel).
 *
 * The description is untrusted human text (PRD §15): react-markdown renders
 * it to React elements — no innerHTML anywhere, and raw HTML inside the
 * markdown is dropped, so an instruction or script hidden in a ticket body
 * stays inert data. Attachments keep their save-only server contract; the
 * download goes through the bearer-authenticated fetch.
 */

import { type FormEvent, useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Link, useParams } from "react-router-dom";
import { api, authFetch } from "../api/client";
import type { Activity, Comment, LinkedMemory, Ticket } from "../api/types";
import ProposalChip from "../components/ProposalChip";
import RecoPanel from "../components/RecoPanel";
import SyncBadge, { type SyncState } from "../components/SyncBadge";
import { recoPanelEnabled } from "../lib/flags";
import { fmtDateTime } from "../lib/format";
import { useSession } from "../lib/session";
import * as ui from "../lib/ui";
import { usePolling } from "../lib/usePolling";
import { VisibilityBadge } from "./Memory";

type TimelineEntry =
  | { kind: "comment"; at: string; comment: Comment }
  | { kind: "activity"; at: string; entry: Activity };

/** Comments + activity, one chronological feed. ``comment.create`` audit rows
 * are skipped — the comment itself is already in the feed. */
function buildTimeline(comments: Comment[], activity: Activity[]): TimelineEntry[] {
  const entries: TimelineEntry[] = [
    ...comments.map(
      (comment): TimelineEntry => ({ kind: "comment", at: comment.created_at, comment }),
    ),
    ...activity
      .filter((entry) => entry.action !== "comment.create")
      .map((entry): TimelineEntry => ({ kind: "activity", at: entry.created_at, entry })),
  ];
  return entries.sort((a, b) => a.at.localeCompare(b.at));
}

/** The field names an update actually changed (ignoring the timestamp). */
function changedFields(entry: Activity): string[] {
  const before = entry.before ?? {};
  const after = entry.after ?? {};
  return Object.keys(after)
    .filter((key) => key !== "updated_at")
    .filter((key) => JSON.stringify(before[key]) !== JSON.stringify(after[key]));
}

export default function TicketPage() {
  const { ticketId } = useParams<{ ticketId: string }>();
  const { connected } = useSession();
  const [ticket, setTicket] = useState<Ticket | null>(null);
  const [comments, setComments] = useState<Comment[]>([]);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [linkedMemory, setLinkedMemory] = useState<LinkedMemory[]>([]);
  const [projectName, setProjectName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!connected || ticketId === undefined) {
      return;
    }
    const path = { params: { path: { ticket_id: ticketId } } };
    const [ticketRes, commentsRes, activityRes, memoryRes] = await Promise.all([
      api.GET("/v1/tickets/{ticket_id}", path),
      api.GET("/v1/tickets/{ticket_id}/comments", path),
      api.GET("/v1/tickets/{ticket_id}/activity", path),
      api.GET("/v1/tickets/{ticket_id}/memory", path),
    ]);
    if (ticketRes.error !== undefined) {
      setError("could not load the ticket");
      return;
    }
    setError(null);
    setTicket(ticketRes.data);
    setComments(commentsRes.data ?? []);
    setActivity(activityRes.data ?? []);
    setLinkedMemory(memoryRes.data ?? []);
  }, [connected, ticketId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  usePolling(() => void refresh(), 2000, connected);

  const projectId = ticket?.project_id ?? null;
  useEffect(() => {
    if (!connected || projectId === null) {
      return;
    }
    void api
      .GET("/v1/projects/{project_id}", { params: { path: { project_id: projectId } } })
      .then(({ data }) => setProjectName(data?.name ?? null));
  }, [connected, projectId]);

  if (!connected) {
    return (
      <section>
        <h1>Ticket</h1>
        <p style={ui.muted}>
          Not connected. Paste your runtime token in <Link to="/settings">Settings</Link> first.
        </p>
      </section>
    );
  }
  if (error !== null) {
    return (
      <section>
        <h1>Ticket</h1>
        <p style={ui.errorText}>{error}</p>
        <p>
          <Link to="/">Back to the backlog</Link>
        </p>
      </section>
    );
  }
  if (ticket === null) {
    return (
      <section>
        <h1>Ticket</h1>
        <p style={ui.muted}>Loading…</p>
      </section>
    );
  }

  const timeline = buildTimeline(comments, activity);

  return (
    <section style={{ display: "flex", gap: "2rem", alignItems: "flex-start" }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <p style={{ margin: 0 }}>
          <Link to="/" style={ui.muted}>
            ← Backlog
          </Link>
        </p>
        <h1 style={{ marginBottom: "0.25rem" }}>{ticket.title}</h1>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: "1rem" }}>
          <span style={ui.chip}>status: {ticket.status}</span>
          <span style={ui.chip}>priority: {ticket.priority}</span>
          <span style={ui.chip}>stage: {ticket.lifecycle_stage}</span>
          {ticket.assignee !== null && <span style={ui.chip}>assignee: {ticket.assignee}</span>}
          {projectName !== null && <span style={ui.chip}>project: {projectName}</span>}
          {ticket.labels.map((label) => (
            <span key={label} style={ui.chip}>
              {label}
            </span>
          ))}
          {ticket.due_date !== null && (
            <span style={ui.chip}>due: {fmtDateTime(ticket.due_date)}</span>
          )}
          {ticket.parent_id !== null && (
            <Link to={`/tickets/${ticket.parent_id}`} style={ui.chip}>
              parent ticket
            </Link>
          )}
        </div>

        <article aria-label="Description">
          {ticket.description.trim() === "" ? (
            <p style={ui.muted}>No description.</p>
          ) : (
            <ReactMarkdown>{ticket.description}</ReactMarkdown>
          )}
        </article>

        {ticket.acceptance_criteria.trim() !== "" && (
          <section aria-labelledby="acceptance-heading">
            <h2 id="acceptance-heading" style={ui.sectionHeading}>
              Acceptance criteria
            </h2>
            <p style={{ whiteSpace: "pre-wrap" }}>{ticket.acceptance_criteria}</p>
          </section>
        )}

        <LinkedMemorySection items={linkedMemory} />

        <Attachments ticket={ticket} onChanged={() => void refresh()} />

        <section aria-labelledby="timeline-heading">
          <h2 id="timeline-heading" style={ui.sectionHeading}>
            Comments & activity
          </h2>
          {timeline.length === 0 && <p style={ui.muted}>Nothing yet.</p>}
          <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 8 }}>
            {timeline.map((item) =>
              item.kind === "comment" ? (
                <li key={`comment-${item.comment.id}`} style={ui.card}>
                  <div style={ui.muted}>
                    {item.comment.author_actor_id} commented · {fmtDateTime(item.at)}
                  </div>
                  <ReactMarkdown>{item.comment.body}</ReactMarkdown>
                </li>
              ) : (
                <li key={`activity-${item.entry.id}`} style={{ ...ui.muted, padding: "0 0.5rem" }}>
                  {item.entry.actor_id} · {item.entry.action}
                  {item.entry.action.endsWith(".update") &&
                    changedFields(item.entry).length > 0 &&
                    `: ${changedFields(item.entry).join(", ")}`}{" "}
                  · {fmtDateTime(item.at)}
                </li>
              ),
            )}
          </ul>
          <CommentComposer ticketId={ticket.id} onPosted={() => void refresh()} />
        </section>
      </div>

      <aside
        aria-label="Ticket status rail"
        style={{ width: recoPanelEnabled() ? 260 : 200, flexShrink: 0, display: "grid", gap: 12 }}
      >
        {recoPanelEnabled() && <RecoPanel ticketId={ticket.id} />}
        <div>
          <div style={ui.label}>Sync</div>
          <SyncBadge state={ticket.sync_state as SyncState} />
        </div>
        <div>
          <div style={ui.label}>Proposals</div>
          {ticket.pending_proposals > 0 ? (
            <Link to="/inbox">
              <ProposalChip count={ticket.pending_proposals} />
            </Link>
          ) : (
            <span style={ui.muted}>none pending</span>
          )}
        </div>
        <div>
          <div style={ui.label}>Created</div>
          <span style={ui.muted}>{fmtDateTime(ticket.created_at)}</span>
        </div>
        <div>
          <div style={ui.label}>Updated</div>
          <span style={ui.muted}>{fmtDateTime(ticket.updated_at)}</span>
        </div>
      </aside>
    </section>
  );
}

function LinkedMemorySection({ items }: { items: LinkedMemory[] }) {
  return (
    <section aria-labelledby="linked-memory-heading">
      <h2 id="linked-memory-heading" style={ui.sectionHeading}>
        Linked memory
      </h2>
      {items.length === 0 && <p style={ui.muted}>None. Link context from the Memory page.</p>}
      <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 8 }}>
        {items.map(({ link, entry }) => (
          <li key={link.id} style={ui.card}>
            <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
              <span style={{ fontWeight: 600 }}>{entry.title}</span>
              <span style={ui.chip}>{entry.type}</span>
              <VisibilityBadge entry={entry} />
            </div>
            {entry.body.trim() !== "" && <ReactMarkdown>{entry.body}</ReactMarkdown>}
            <div style={ui.muted}>
              linked because: {link.reason} · from {entry.provenance.origin ?? entry.source} by{" "}
              {entry.provenance.actor_id ?? entry.created_by ?? "unknown"}
              {entry.provenance.captured_at !== undefined &&
                ` · captured ${fmtDateTime(entry.provenance.captured_at)}`}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Attachments({ ticket, onChanged }: { ticket: Ticket; onChanged: () => void }) {
  const [error, setError] = useState<string | null>(null);

  async function download(blobId: string, filename: string) {
    const response = await authFetch(`/v1/tickets/${ticket.id}/attachments/${blobId}`);
    if (!response.ok) {
      setError("download failed");
      return;
    }
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  async function upload(file: File) {
    const body = new FormData();
    body.append("file", file, file.name);
    const response = await authFetch(`/v1/tickets/${ticket.id}/attachments`, {
      method: "POST",
      body,
    });
    if (!response.ok) {
      setError(response.status === 413 ? "file exceeds the 10 MiB limit" : "upload failed");
      return;
    }
    setError(null);
    onChanged();
  }

  return (
    <section aria-labelledby="attachments-heading">
      <h2 id="attachments-heading" style={ui.sectionHeading}>
        Attachments
      </h2>
      {ticket.attachments.length === 0 && <p style={ui.muted}>None.</p>}
      <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: 4 }}>
        {ticket.attachments.map((attachment) => (
          <li key={attachment.blob_id}>
            <button
              type="button"
              style={{ ...ui.button, fontWeight: 400 }}
              onClick={() => void download(attachment.blob_id, attachment.filename)}
            >
              {attachment.filename}
            </button>{" "}
            <span style={ui.muted}>
              {attachment.media_type} · {attachment.size_bytes} bytes (saves as a file; never
              rendered)
            </span>
          </li>
        ))}
      </ul>
      <label style={{ ...ui.label, marginTop: 8 }}>
        Add attachment
        <input
          type="file"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file !== undefined) {
              void upload(file);
              event.target.value = "";
            }
          }}
        />
      </label>
      {error !== null && <p style={ui.errorText}>{error}</p>}
    </section>
  );
}

function CommentComposer({ ticketId, onPosted }: { ticketId: string; onPosted: () => void }) {
  const [body, setBody] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function post(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!body.trim()) {
      return;
    }
    const { error: apiError } = await api.POST("/v1/tickets/{ticket_id}/comments", {
      params: { path: { ticket_id: ticketId } },
      body: { body: body.trim() },
    });
    if (apiError !== undefined) {
      setError("could not post the comment");
      return;
    }
    setError(null);
    setBody("");
    onPosted();
  }

  return (
    <form onSubmit={post} style={{ marginTop: "0.75rem", display: "grid", gap: 8 }}>
      <label style={ui.label}>
        Add a comment
        <textarea
          style={{ ...ui.input, minHeight: "4rem", resize: "vertical" }}
          value={body}
          onChange={(event) => setBody(event.target.value)}
        />
      </label>
      <div>
        <button type="submit" style={ui.primaryButton}>
          Comment
        </button>{" "}
        {error !== null && <span style={ui.errorText}>{error}</span>}
      </div>
    </form>
  );
}
