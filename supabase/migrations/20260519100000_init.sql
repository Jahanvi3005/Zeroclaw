create table if not exists container_registry (
    user_id uuid primary key references auth.users(id),
    container_id text not null,
    ws_url text not null,
    http_url text not null,
    bearer_token_enc text not null,
    status text not null default 'provisioning',
    created_at timestamptz default now(),
    last_active_at timestamptz default now()
);

comment on column container_registry.bearer_token_enc
    is 'Fernet-encrypted bearer token for the ZeroClaw container';
comment on column container_registry.status
    is 'provisioning | ready | stopping | stopped';

alter table container_registry add column if not exists current_session_id text;

comment on column container_registry.current_session_id
    is 'ZeroClaw session ID to resume on next WS connect';
