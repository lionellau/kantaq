-- =====================================================================
-- v0.3 live verify (READ-ONLY) — run after the apply; expect no >>> MISSING <<<
-- =====================================================================

-- The 3 new tables exist + RLS is on
with expected(t) as (values ('milestones'),('ticket_milestones'),('follow_ups'))
select e.t as expected_table,
  case when c.oid is null then '>>> MISSING <<<' else 'present' end as exists,
  case when c.oid is null then '-'
       when c.relrowsecurity then 'on' else '>>> RLS OFF <<<' end as rls
from expected e
left join pg_class c
  on c.relname = e.t and c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
order by e.t;

-- The helper functions the new policies depend on
select obj as expected_function,
  case when exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                    where n.nspname || '.' || p.proname = obj)
       then 'present' else '>>> MISSING <<<' end as exists
from (values ('kantaq.project_in_my_workspaces'),
             ('kantaq.milestone_in_my_workspaces'),
             ('kantaq.ticket_in_my_workspaces')) f(obj)
order by obj;

-- The 3 new collections are in the sync allowlist
select t as collection,
  case when pg_get_constraintdef((select oid from pg_constraint
         where conname = 'ck_sync_events_collection')) like '%' || t || '%'
       then 'in allowlist' else '>>> MISSING <<<' end as status
from (values ('milestones'),('ticket_milestones'),('follow_ups')) f(t)
order by t;
