-- The Data Wrangler: Phase 3 / 4 database setup.
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run. Safe to run twice.

-- 0. From the email-token feature (skip if already done)
alter table public.sasams_schools add column if not exists contact_email text;

-- 1. Term snapshots + interventions. The school_id column copies the type of
--    sasams_schools.id automatically (uuid or bigint), so this works either way.
do $$
declare idtype text;
begin
  select format_type(a.atttypid, a.atttypmod) into idtype
  from pg_attribute a where a.attrelid = 'public.sasams_schools'::regclass and a.attname = 'id';

  execute format($f$
    create table if not exists public.sasams_student_history (
      id bigint generated always as identity primary key,
      school_id %s not null references public.sasams_schools(id) on delete cascade,
      id_number text not null,
      subject text not null,
      grade int,
      sba_mark numeric,
      attendance_rate numeric,
      year int not null,
      term int not null check (term between 1 and 4),
      created_at timestamptz not null default now(),
      unique (school_id, id_number, subject, year, term)
    )$f$, idtype);

  execute format($f$
    create table if not exists public.sasams_interventions (
      id bigint generated always as identity primary key,
      school_id %s not null references public.sasams_schools(id) on delete cascade,
      id_number text not null,
      created_at timestamptz not null default now()
    )$f$, idtype);

  -- School Report: per-term validation counts and the saved diagnosis
  execute format($f$
    create table if not exists public.sasams_term_reports (
      id bigint generated always as identity primary key,
      school_id %s not null references public.sasams_schools(id) on delete cascade,
      year int not null,
      term int not null check (term between 1 and 4),
      validation jsonb,
      diagnosis jsonb,
      diagnosis_source text,
      diagnosis_model text,
      updated_at timestamptz not null default now(),
      unique (school_id, year, term)
    )$f$, idtype);
end $$;

-- If sasams_interventions already existed from an earlier draft, these add what is missing.
alter table public.sasams_interventions add column if not exists title text;
alter table public.sasams_interventions add column if not exists category text default 'Academic';
alter table public.sasams_interventions add column if not exists status text default 'planned';
alter table public.sasams_interventions add column if not exists notes text;
alter table public.sasams_interventions add column if not exists outcome text;
alter table public.sasams_interventions add column if not exists due_date date;
alter table public.sasams_interventions add column if not exists year int;
alter table public.sasams_interventions add column if not exists term int;
alter table public.sasams_interventions add column if not exists updated_at timestamptz;
alter table public.sasams_interventions add column if not exists completed_at timestamptz;
-- (If an older sasams_interventions has extra NOT NULL columns and no rows, the cleanest fix is:
--  drop table public.sasams_interventions;  then run this whole script again.)

create index if not exists history_school_period on public.sasams_student_history (school_id, year, term);
create index if not exists interventions_school_status on public.sasams_interventions (school_id, status);

-- 2. Lock the new tables. RLS on + NO policies = the public anon key can read nothing.
--    The server uses the service_role key, which bypasses RLS, and filters every
--    query by the school's id in code.
alter table public.sasams_student_history enable row level security;
alter table public.sasams_interventions enable row level security;
alter table public.sasams_term_reports enable row level security;

-- 3. PHASE 4 HARDENING: run ONLY after Render's SUPABASE_KEY is the service_role key
--    and the app still works (register, login, upload). Then the old open policies
--    are no longer needed and should go, so nobody can use the anon key to read or write schools/learners.
-- drop policy if exists "Allow API Insert"  on public.sasams_schools;
-- drop policy if exists "Allow API Select"  on public.sasams_schools;
-- drop policy if exists "Allow API Inserts" on public.sasams_students;
-- drop policy if exists "Allow API Select"  on public.sasams_students;
-- drop policy if exists "Allow API Updates" on public.sasams_students;
