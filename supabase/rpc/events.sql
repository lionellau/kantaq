-- supabase/rpc/events.sql
-- HAND-WRITTEN (E24-T6, FR-E24-3, D-09): the v0.2 atomic commit RPC.
-- Apply AFTER 0002_sync_events.sql + policies/0002_sync_rls.sql (it reads the
-- kantaq.* helpers and the sync_events table) and AFTER 0001_collections.sql
-- (it reads capability_grants). Re-appliable as-is (create or replace).
--
-- WHAT THIS CLOSES (MOD-05 line 45, D-09): in v0.0.5/v0.1 the sync transport is
-- Supabase's PostgREST data API — an INSERT whose identity column assigns the
-- `revision` at INSERT but exposes it at COMMIT, so under concurrent pushes a
-- reader can observe revision N+1 before an in-flight N. This RPC assigns the
-- revision, applies the merge policy, and commits in ONE transaction, serialised
-- per workspace by an advisory xact lock, so the commit-visibility window is
-- closed *among RPC callers*: N is fully committed before N+1 is even assigned.
-- (The v0.1 raw-table push path bypasses this lock; it is retired as the runtime
-- cuts over to commit_events — until then both must not run concurrently.)
--
-- WHERE THE SIGNATURE CHECK LIVES (D-09, recorded in docs/stack.md + MOD-05):
-- stock Postgres has NO Ed25519 primitive (pgsodium is a Supabase-only,
-- pending-deprecation extension, absent from the CI/self-host Postgres), so the
-- cryptographic byte-verification of the Ed25519 signature stays CLIENT-SIDE at
-- the VerifyingBackend edge (kantaq_sync_engine.verify), exactly as in v0.1.
-- This RPC enforces, server-side and atomically, EVERYTHING ELSE that v0.1's
-- verifier checks and that is decidable against committed state: signature
-- *presence* (when the caller passes p_require_signature — a defense-in-depth
-- check, not a server-enforced cutover; the authoritative signed-sync wall is
-- the peer's pull-side VerifyingBackend), the grant (held, live issuer device,
-- not revoked, valid window, subject == actor, resource == workspace, a verb
-- authorising the collection), commit ordering, and revision assignment.
-- MOD-17's honest-naming rule: we do not claim the RPC verifies a signature it
-- cannot, nor a cutover it does not hold. The self-hosted
-- adapter (MOD-28) reuses the same merge decision in Python; a pgsodium-capable
-- deployment could add the byte-check here later without changing the contract.
--
-- MERGE POLICY (D-05): last-writer-wins by server commit order. The RPC also
-- reports `stale_base_rev` when an event's base_rev is older than the committed
-- head for its entity, so a committing client can mint a signed conflict_record
-- (E05-T2 / MOD-26 — that collection + the per-field conflict engine are the
-- sibling task; this RPC supplies the metadata they consume). base_rev NULL is
-- treated as genesis (B = 0).
--
-- NOTE (Year-2038): capability_grants.issued_at/expires_at are 32-bit INTEGER
-- unix seconds (the v0.1 schema), so the window check below inherits a 2038
-- ceiling. Widening them to BIGINT is a follow-up schema change, out of E24-T6
-- scope (tracked as a downstream concern).

-- ---------------------------------------------------------------------------
-- Helper: the grant verbs that authorise a write to each syncable collection.
-- Mirrors kantaq_sync_engine.verify._COLLECTION_WRITE_VERBS field-for-field
-- (pinned equal by tests/test_verb_map_parity.py). A collection that returns
-- NULL is not verb-checked — the syncable allowlist (the CHECK + SYNCABLE_MODELS)
-- bounds the collection set, and the trust roots (devices/capability_grants)
-- defer their per-verb model to DEBT-15(a/b). SECURITY-DEFINER-clean: immutable,
-- empty search_path.
-- ---------------------------------------------------------------------------

create or replace function kantaq.collection_write_verbs(p_collection varchar)
returns text[]
language sql immutable
set search_path = ''
as $$
  select case p_collection
    when 'workspaces' then array['tickets.write', 'members.invite']
    when 'projects' then array['tickets.write']
    when 'tickets' then array['tickets.write']
    when 'comments' then array['tickets.write']
    when 'ticket_relationships' then array['tickets.write']
    when 'members' then array['members.invite', 'members.revoke']
    when 'agent_proposals' then array['proposals.write', 'tickets.write']
    when 'memory_entries' then array['memory.write']
    when 'memory_links' then array['memory.write']
    else null
  end
$$;

-- ---------------------------------------------------------------------------
-- public.events — the atomic commit RPC, callable via PostgREST as
-- POST /rest/v1/rpc/events with body {"p_events": [<event>...],
-- "p_require_signature": <bool>}. SECURITY DEFINER (it writes the append-only
-- log and reads grants under its own authority) but does its OWN authorisation
-- internally against the caller's JWT — it cannot live in the un-exposed kantaq
-- schema. Returns a JSONB array, one result object per submitted event.
-- ---------------------------------------------------------------------------

create or replace function public.events(p_events jsonb, p_require_signature boolean default true)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  e            jsonb;
  v_actor      varchar;
  v_ws         varchar;
  v_collection varchar;
  v_entity     varchar;
  v_seq        integer;
  v_op         varchar;
  v_base       bigint;
  v_policy     varchar;
  v_sig        varchar;
  v_event_id   varchar;
  v_now        bigint := floor(extract(epoch from now()))::bigint;
  v_verbs      text[];
  g            public.capability_grants%rowtype;
  v_head       bigint;
  v_rev        bigint;
  v_status     text;
  v_stale      bigint;
  results      jsonb := '[]'::jsonb;
begin
  -- ----------------------------------------------------------------- pass 1
  -- Validate EVERY event before committing ANY (atomic reject — mirrors the
  -- v0.1 VerifyingBackend.push: a single failure submits nothing). A RAISE
  -- here rolls the whole function back, so PostgREST returns the structured
  -- error and the log is untouched.
  for e in select * from jsonb_array_elements(p_events)
  loop
    v_actor      := e ->> 'actor_id';
    v_ws         := e ->> 'workspace_id';
    v_collection := e ->> 'collection';
    v_sig        := e ->> 'sig';
    v_policy     := e ->> 'policy_ref';
    v_event_id   := e ->> 'event_id';

    -- The actor must be the signed-in caller, active in this workspace — the
    -- same wall as sync_events_insert RLS, re-checked here because a SECURITY
    -- DEFINER function bypasses RLS.
    if not kantaq.is_self_in_workspace(v_actor, v_ws) then
      raise exception 'policy_denied: actor % is not the caller acting in workspace %', v_actor, v_ws
        using errcode = '42501';
    end if;

    if v_sig is null then
      if p_require_signature then
        raise exception 'unsigned: event % carries no signature', v_event_id
          using errcode = '42501';
      end if;
      -- pre-cutover unsigned event: tolerated history, no grant to check.
      continue;
    end if;

    -- Signed → the grant must resolve and authorise this write (the verify.py
    -- check order: held → not revoked → window → subject → resource → verb).
    if v_policy is null then
      raise exception 'policy_denied: event % names no grant', v_event_id using errcode = '42501';
    end if;
    select * into g from public.capability_grants where id = v_policy;
    if not found then
      raise exception 'policy_denied: grant % is not held', v_policy using errcode = '42501';
    end if;
    -- The issuing device must be a live verification root — verify_grant resolves
    -- the issuer against `roots` and a revoked device can issue nothing (the
    -- primary trust wall, grants.py). Mirror it: absent or revoked issuer device
    -- → deny, before trusting the grant.
    if not exists (
      select 1 from public.devices d where d.id = g.issuer and d.revoked_at is null
    ) then
      raise exception 'policy_denied: grant % issuer device is not a live root', v_policy
        using errcode = '42501';
    end if;
    if g.revoked_at is not null then
      raise exception 'policy_denied: grant % is revoked', v_policy using errcode = '42501';
    end if;
    -- An inverted window (expires <= issued) is a malformed grant — verify_grant
    -- rejects it as invalid_validity before the time checks.
    if g.expires_at <= g.issued_at then
      raise exception 'policy_denied: grant % has an invalid validity window', v_policy
        using errcode = '42501';
    end if;
    if v_now < g.issued_at then
      raise exception 'policy_denied: grant % is not yet valid', v_policy using errcode = '42501';
    end if;
    if v_now >= g.expires_at then
      raise exception 'policy_denied: grant % is expired', v_policy using errcode = '42501';
    end if;
    if g.subject <> v_actor then
      raise exception 'policy_denied: grant % does not authorise actor %', v_policy, v_actor
        using errcode = '42501';
    end if;
    if g.resource <> v_ws then
      raise exception 'policy_denied: grant % does not scope workspace %', v_policy, v_ws
        using errcode = '42501';
    end if;
    v_verbs := kantaq.collection_write_verbs(v_collection);
    if v_verbs is not null and not (g.verbs::jsonb ?| v_verbs) then
      raise exception 'policy_denied: grant % does not authorise writes to %', v_policy, v_collection
        using errcode = '42501';
    end if;
  end loop;

  -- Acquire every workspace's advisory xact lock UP FRONT, in a deterministic
  -- (sorted) order, so two concurrent multi-workspace batches can never deadlock
  -- by taking the same locks in opposite orders. The locks auto-release at
  -- COMMIT. A single-workspace batch (the only caller today) takes one lock.
  for v_ws in
    select distinct x ->> 'workspace_id'
      from jsonb_array_elements(p_events) x
      order by 1
  loop
    perform pg_advisory_xact_lock(hashtext('kantaq.sync_events:' || v_ws));
  end loop;

  -- ----------------------------------------------------------------- pass 2
  -- Commit in submission order. Because every workspace lock is already held,
  -- revision N commits fully before N+1 is assigned, closing the v0.1
  -- commit-visibility window among RPC callers.
  for e in select * from jsonb_array_elements(p_events)
  loop
    v_actor      := e ->> 'actor_id';
    v_ws         := e ->> 'workspace_id';
    v_collection := e ->> 'collection';
    v_entity     := e ->> 'entity_id';
    v_seq        := (e ->> 'actor_seq')::integer;
    v_op         := e ->> 'op';
    v_base       := nullif(e ->> 'base_rev', '')::bigint;
    v_policy     := e ->> 'policy_ref';
    v_sig        := e ->> 'sig';
    v_event_id   := e ->> 'event_id';

    -- The committed head for this entity (LWW by commit order, D-05).
    select coalesce(max(revision), 0) into v_head
      from public.sync_events
      where workspace_id = v_ws and collection = v_collection and entity_id = v_entity;

    -- Staleness (v0.2): a base_rev older than the committed head means another
    -- write landed first. The event still commits (LWW by order), but the
    -- result reports stale_base_rev so the client can mint a conflict_record
    -- (E05-T2). A NULL base_rev is genesis (B = 0) and never stale.
    if v_base is not null and v_base < v_head then
      v_stale := v_base;
    else
      v_stale := null;
    end if;

    insert into public.sync_events
      (event_id, collection, entity_id, actor_id, actor_seq, op,
       base_rev, policy_ref, payload, sig, workspace_id)
    values
      (v_event_id, v_collection, v_entity, v_actor, v_seq, v_op,
       v_base, v_policy, (e -> 'payload')::json, v_sig, v_ws)
    on conflict (actor_id, actor_seq) do nothing
    returning revision into v_rev;

    if v_rev is null then
      -- Dedup floor hit (idempotent re-push): return the already-committed
      -- revision. This event did NOT commit now, so the merge metadata
      -- (head/base/stale) is not meaningful for it — report it as null.
      select revision into v_rev from public.sync_events
        where actor_id = v_actor and actor_seq = v_seq;
      v_status := 'duplicate';
      v_stale := null;
      v_head := null;
    else
      v_status := 'committed';
    end if;

    results := results || jsonb_build_object(
      'event_id', v_event_id,
      'status', v_status,
      'revision', v_rev,
      'base_rev', case when v_status = 'duplicate' then null else v_base end,
      'head_rev', v_head,
      'stale_base_rev', v_stale
    );
  end loop;

  return results;
end;
$$;

-- The RPC is the MEMBER commit path: callable only by `authenticated`, whose
-- JWT email the internal is_self_in_workspace check binds to the acting member.
-- anon (Auth-only) never reaches it. service_role is deliberately NOT granted
-- EXECUTE: it has no JWT email so the self-check would reject every event, and
-- the backend has no need to commit on a member's behalf in v0.2 (a future
-- service-side ingest would branch the authz explicitly and be documented then).
-- PUBLIC is stripped so a future role cannot inherit EXECUTE.
revoke all on function public.events(jsonb, boolean) from public;
grant execute on function public.events(jsonb, boolean) to authenticated;
