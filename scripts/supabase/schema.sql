-- Run in Supabase SQL Editor (Dashboard → SQL → New query)
-- Creates the inventory table for cross-device card tracking.

create table if not exists inventory (
  sync_code text not null,
  card_id text not null,
  quantity integer not null default 0 check (quantity >= 0),
  updated_at timestamptz not null default now(),
  primary key (sync_code, card_id)
);

create index if not exists inventory_sync_code_idx on inventory (sync_code);

alter table inventory enable row level security;

drop policy if exists "open_access" on inventory;
create policy "open_access" on inventory
  for all
  using (true)
  with check (true);

-- Optional: auto-update updated_at on change
create or replace function update_inventory_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists inventory_updated_at on inventory;
create trigger inventory_updated_at
  before update on inventory
  for each row execute function update_inventory_updated_at();

-- Enable Realtime (required for live sync across devices):
-- Dashboard → Database → Replication → enable "inventory" table
-- Or run:
alter table inventory replica identity full;
