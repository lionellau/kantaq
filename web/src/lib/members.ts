/**
 * E20-T6 (MOD-12) — the member directory: resolve actor ULIDs to people.
 *
 * An approver decides between *people*, not 26-char identifiers. The Inbox
 * fetches the member list once and hands a id→member map to its cards so a
 * proposer / conflict actor / promoter renders as a name (email), with the raw
 * ULID kept on hover for the audit-minded. A miss (a since-removed member, or a
 * non-member entity id) falls back to the raw id — honest, never a guess.
 */

import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Member } from "../api/types";

export type MemberDirectory = Map<string, Member>;

export function useMemberDirectory(connected: boolean): MemberDirectory {
  const [directory, setDirectory] = useState<MemberDirectory>(new Map());

  useEffect(() => {
    if (!connected) {
      return;
    }
    void api.GET("/v1/members").then(({ data }) => {
      if (data !== undefined) {
        setDirectory(new Map(data.map((member) => [member.id, member])));
      }
    });
  }, [connected]);

  return directory;
}
