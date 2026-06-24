/**
 * E20-T6 (MOD-12) — render an actor ULID as a person.
 *
 * Resolves an id against the member directory and shows the member's email (and
 * an "agent" tag for Agent-role members), with the raw ULID kept in the `title`
 * for the expand/detail an auditor needs. An unknown id (removed member, or a
 * non-member entity id) renders raw and monospaced — honest, never invented.
 */

import type { MemberDirectory } from "../lib/members";
import { font } from "../lib/ui";

export default function ActorName({
  id,
  directory,
}: {
  id: string;
  directory: MemberDirectory;
}) {
  const member = directory.get(id);
  if (member === undefined) {
    return (
      <span title={id} style={{ fontFamily: font.mono, fontSize: "0.85em" }}>
        {id}
      </span>
    );
  }
  const isAgent = member.role === "Agent";
  return (
    <span title={id}>
      {member.email}
      {isAgent && <span> (agent)</span>}
    </span>
  );
}
