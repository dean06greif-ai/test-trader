-- Supabase-Schema für den KI-Wissensspeicher (KI-Gedächtnis).
-- Einmalig im Supabase SQL-Editor ausführen. Der Zugriff erfolgt ausschließlich
-- serverseitig mit dem Service-/Secret-Key (SUPABASE_SERVICE_ROLE_KEY).

create table if not exists public.ai_knowledge (
    id        text primary key,
    kind      text not null,
    title     text not null,
    content   text,
    meta      jsonb default '{}'::jsonb,
    tags      text[] default '{}',
    weight    int  default 2,
    source    text,
    ts        timestamptz not null default now()
);

create index if not exists ai_knowledge_kind_ts_idx on public.ai_knowledge (kind, ts desc);
create index if not exists ai_knowledge_ts_idx       on public.ai_knowledge (ts desc);
create index if not exists ai_knowledge_tags_idx     on public.ai_knowledge using gin (tags);

-- RLS an, aber keine Policy: nur der Service-Key (bypasst RLS) darf lesen/schreiben.
alter table public.ai_knowledge enable row level security;
