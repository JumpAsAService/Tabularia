-- The development database's schema on 2026-10-10, BEFORE Alembic (pg_dump -s): create_all
-- plus the idempotent ALTERs, with the drift that mechanism left. It serves to test the
-- adoption (test_migrations.py). Schema only: no data.
CREATE TABLE public.ai_chat_turns (
    id integer NOT NULL,
    chat_id integer NOT NULL,
    seq integer NOT NULL,
    question character varying NOT NULL,
    messages character varying NOT NULL,
    model_id character varying NOT NULL,
    input_tokens integer NOT NULL,
    output_tokens integer NOT NULL,
    requests integer NOT NULL,
    cost_usd character varying,
    created_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.ai_chat_turns_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.ai_chat_turns_id_seq OWNED BY public.ai_chat_turns.id;
CREATE TABLE public.ai_chats (
    id integer NOT NULL,
    user_id integer NOT NULL,
    title character varying NOT NULL,
    model_id character varying NOT NULL,
    engine character varying,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.ai_chats_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.ai_chats_id_seq OWNED BY public.ai_chats.id;
CREATE TABLE public.ai_models (
    model_id character varying NOT NULL,
    enabled boolean NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    updated_by integer
);
CREATE TABLE public.ai_spend (
    id integer NOT NULL,
    ts timestamp without time zone NOT NULL,
    user_id integer NOT NULL,
    turn_id integer,
    model_id character varying NOT NULL,
    input_tokens integer NOT NULL,
    output_tokens integer NOT NULL,
    cost_usd character varying
);
CREATE SEQUENCE public.ai_spend_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.ai_spend_id_seq OWNED BY public.ai_spend.id;
CREATE TABLE public.audit_logs (
    id integer NOT NULL,
    ts timestamp without time zone NOT NULL,
    actor_id integer,
    actor_label character varying NOT NULL,
    action character varying NOT NULL,
    outcome character varying NOT NULL,
    target_type character varying,
    target_id integer,
    target_label character varying,
    detail text,
    ip character varying,
    user_agent text
);
CREATE SEQUENCE public.audit_logs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.audit_logs_id_seq OWNED BY public.audit_logs.id;
CREATE TABLE public.banners (
    id integer NOT NULL,
    message text NOT NULL,
    level character varying NOT NULL,
    created_at timestamp without time zone NOT NULL,
    created_by integer
);
CREATE SEQUENCE public.banners_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.banners_id_seq OWNED BY public.banners.id;
CREATE TABLE public.connections (
    id integer NOT NULL,
    name character varying NOT NULL,
    description character varying NOT NULL,
    project_id integer NOT NULL,
    owner_id integer,
    db_type character varying NOT NULL,
    host character varying NOT NULL,
    port integer,
    username character varying NOT NULL,
    password_encrypted text NOT NULL,
    database character varying NOT NULL,
    db_schema character varying NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    extra text DEFAULT '{}'::text NOT NULL
);
CREATE SEQUENCE public.connections_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.connections_id_seq OWNED BY public.connections.id;
CREATE TABLE public.data_contract_results (
    id integer NOT NULL,
    datasource_id integer NOT NULL,
    contract_version integer NOT NULL,
    trigger character varying NOT NULL,
    run_id integer,
    snapshot_key character varying NOT NULL,
    outcome character varying NOT NULL,
    blocked boolean NOT NULL,
    errors integer NOT NULL,
    warnings integer NOT NULL,
    rows integer,
    report text NOT NULL,
    notify character varying,
    evaluated_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.data_contract_results_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.data_contract_results_id_seq OWNED BY public.data_contract_results.id;
CREATE TABLE public.data_contract_versions (
    id integer NOT NULL,
    datasource_id integer NOT NULL,
    version integer NOT NULL,
    document text NOT NULL,
    created_by integer,
    created_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.data_contract_versions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.data_contract_versions_id_seq OWNED BY public.data_contract_versions.id;
CREATE TABLE public.data_contracts (
    id integer NOT NULL,
    datasource_id integer NOT NULL,
    version integer NOT NULL,
    document text NOT NULL,
    enabled boolean NOT NULL,
    status character varying NOT NULL,
    checked_at timestamp without time zone,
    errors integer NOT NULL,
    warnings integer NOT NULL,
    last_report text,
    blocked_at timestamp without time zone,
    blocked_run_id integer,
    notify_emails character varying,
    notify_connection_id integer,
    created_by integer,
    updated_by integer,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.data_contracts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.data_contracts_id_seq OWNED BY public.data_contracts.id;
CREATE TABLE public.datasources (
    id integer NOT NULL,
    name character varying NOT NULL,
    description character varying NOT NULL,
    project_id integer NOT NULL,
    owner_id integer,
    bucket character varying NOT NULL,
    key character varying NOT NULL,
    rows integer,
    columns text NOT NULL,
    column_descriptions text NOT NULL,
    kind character varying NOT NULL,
    flow_id integer,
    connection_id integer,
    source_type character varying,
    source_ref text,
    refreshed_at timestamp without time zone,
    refresh_schedule character varying,
    refresh_scheduled_by integer,
    next_refresh_at timestamp without time zone,
    snapshot_run_id integer,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    sort_keys text DEFAULT '[]'::text NOT NULL
);
CREATE SEQUENCE public.datasources_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.datasources_id_seq OWNED BY public.datasources.id;
CREATE TABLE public.dbt_ai_texts (
    id integer NOT NULL,
    flow_id integer NOT NULL,
    flow_version integer NOT NULL,
    kind character varying NOT NULL,
    key character varying(500) NOT NULL,
    text text NOT NULL,
    model_id character varying NOT NULL,
    created_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.dbt_ai_texts_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.dbt_ai_texts_id_seq OWNED BY public.dbt_ai_texts.id;
CREATE TABLE public.disabled_engines (
    engine_id character varying NOT NULL,
    disabled_at timestamp without time zone NOT NULL,
    disabled_by integer
);
CREATE TABLE public.flow_presence (
    flow_id integer NOT NULL,
    instance character varying NOT NULL,
    user_id integer NOT NULL,
    email character varying NOT NULL,
    full_name character varying NOT NULL,
    since double precision NOT NULL,
    seen double precision NOT NULL
);
CREATE TABLE public.flow_versions (
    id integer NOT NULL,
    flow_id integer NOT NULL,
    version integer NOT NULL,
    definition text NOT NULL,
    note character varying NOT NULL,
    created_at timestamp without time zone NOT NULL,
    created_by integer
);
CREATE SEQUENCE public.flow_versions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.flow_versions_id_seq OWNED BY public.flow_versions.id;
CREATE TABLE public.flows (
    id integer NOT NULL,
    name character varying NOT NULL,
    description character varying NOT NULL,
    project_id integer NOT NULL,
    owner_id integer,
    definition text NOT NULL,
    engine character varying NOT NULL,
    production_engine character varying,
    run_schedule character varying,
    run_scheduled_by integer,
    next_run_at timestamp without time zone,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    notify_emails text,
    notify_connection_id integer
);
CREATE SEQUENCE public.flows_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.flows_id_seq OWNED BY public.flows.id;
CREATE TABLE public.groups (
    id integer NOT NULL,
    name character varying NOT NULL,
    description character varying NOT NULL,
    created_at timestamp without time zone NOT NULL,
    is_admin boolean DEFAULT false NOT NULL,
    is_observer boolean DEFAULT false NOT NULL
);
CREATE SEQUENCE public.groups_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.groups_id_seq OWNED BY public.groups.id;
CREATE TABLE public.login_attempts (
    ip character varying NOT NULL,
    email character varying NOT NULL,
    failures integer NOT NULL,
    last_at timestamp without time zone NOT NULL
);
CREATE TABLE public.pending_blob_deletions (
    id integer NOT NULL,
    bucket character varying NOT NULL,
    key character varying NOT NULL,
    delete_after timestamp without time zone NOT NULL,
    reason character varying NOT NULL,
    created_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.pending_blob_deletions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.pending_blob_deletions_id_seq OWNED BY public.pending_blob_deletions.id;
CREATE TABLE public.permissions (
    id integer NOT NULL,
    project_id integer NOT NULL,
    user_id integer,
    group_id integer,
    capability character varying NOT NULL
);
CREATE SEQUENCE public.permissions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.permissions_id_seq OWNED BY public.permissions.id;
CREATE TABLE public.privacy_notice (
    id integer NOT NULL,
    enabled boolean NOT NULL,
    summary text NOT NULL,
    body text NOT NULL,
    url character varying NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    updated_by integer
);
CREATE TABLE public.projects (
    id integer NOT NULL,
    name character varying NOT NULL,
    description character varying NOT NULL,
    parent_id integer,
    owner_id integer,
    created_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.projects_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.projects_id_seq OWNED BY public.projects.id;
CREATE TABLE public.runs (
    id integer NOT NULL,
    kind character varying NOT NULL,
    flow_id integer,
    task_id character varying NOT NULL,
    status character varying NOT NULL,
    launched_by integer,
    trigger_type character varying NOT NULL,
    engine character varying,
    parent_run_id integer,
    input_key character varying NOT NULL,
    output_bucket character varying NOT NULL,
    output_key character varying NOT NULL,
    rows_written integer,
    error text,
    error_detail text,
    publish_name character varying,
    publish_project_id integer,
    publish_description character varying NOT NULL,
    publish_overwrite boolean NOT NULL,
    datasource_id integer,
    destination text,
    started_at timestamp without time zone NOT NULL,
    engine_started_at timestamp without time zone,
    finished_at timestamp without time zone,
    mirror text,
    email text,
    publish_sort_keys character varying DEFAULT '[]'::character varying NOT NULL,
    engine_mode character varying,
    claimed_by character varying,
    heartbeat_at timestamp without time zone
);
CREATE SEQUENCE public.runs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.runs_id_seq OWNED BY public.runs.id;
CREATE TABLE public.saved_views (
    id integer NOT NULL,
    name character varying NOT NULL,
    description character varying NOT NULL,
    project_id integer NOT NULL,
    owner_id integer,
    datasource_id integer NOT NULL,
    spec text NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.saved_views_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.saved_views_id_seq OWNED BY public.saved_views.id;
CREATE TABLE public.schema_state (
    key character varying NOT NULL,
    value character varying NOT NULL
);
CREATE TABLE public.uploads (
    id integer NOT NULL,
    dataset_id character varying NOT NULL,
    bucket character varying NOT NULL,
    parquet_key character varying NOT NULL,
    raw_key character varying NOT NULL,
    filename character varying NOT NULL,
    owner_id integer,
    created_at timestamp without time zone NOT NULL
);
CREATE SEQUENCE public.uploads_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.uploads_id_seq OWNED BY public.uploads.id;
CREATE TABLE public.user_groups (
    user_id integer NOT NULL,
    group_id integer NOT NULL
);
CREATE TABLE public.users (
    id integer NOT NULL,
    email character varying NOT NULL,
    full_name character varying NOT NULL,
    hashed_password character varying,
    is_active boolean NOT NULL,
    is_superuser boolean NOT NULL,
    created_at timestamp without time zone NOT NULL,
    last_seen_at timestamp without time zone,
    last_seen_ip character varying,
    oidc_issuer character varying,
    oidc_subject character varying,
    is_observer boolean DEFAULT false NOT NULL
);
CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;
ALTER TABLE ONLY public.ai_chat_turns ALTER COLUMN id SET DEFAULT nextval('public.ai_chat_turns_id_seq'::regclass);
ALTER TABLE ONLY public.ai_chats ALTER COLUMN id SET DEFAULT nextval('public.ai_chats_id_seq'::regclass);
ALTER TABLE ONLY public.ai_spend ALTER COLUMN id SET DEFAULT nextval('public.ai_spend_id_seq'::regclass);
ALTER TABLE ONLY public.audit_logs ALTER COLUMN id SET DEFAULT nextval('public.audit_logs_id_seq'::regclass);
ALTER TABLE ONLY public.banners ALTER COLUMN id SET DEFAULT nextval('public.banners_id_seq'::regclass);
ALTER TABLE ONLY public.connections ALTER COLUMN id SET DEFAULT nextval('public.connections_id_seq'::regclass);
ALTER TABLE ONLY public.data_contract_results ALTER COLUMN id SET DEFAULT nextval('public.data_contract_results_id_seq'::regclass);
ALTER TABLE ONLY public.data_contract_versions ALTER COLUMN id SET DEFAULT nextval('public.data_contract_versions_id_seq'::regclass);
ALTER TABLE ONLY public.data_contracts ALTER COLUMN id SET DEFAULT nextval('public.data_contracts_id_seq'::regclass);
ALTER TABLE ONLY public.datasources ALTER COLUMN id SET DEFAULT nextval('public.datasources_id_seq'::regclass);
ALTER TABLE ONLY public.dbt_ai_texts ALTER COLUMN id SET DEFAULT nextval('public.dbt_ai_texts_id_seq'::regclass);
ALTER TABLE ONLY public.flow_versions ALTER COLUMN id SET DEFAULT nextval('public.flow_versions_id_seq'::regclass);
ALTER TABLE ONLY public.flows ALTER COLUMN id SET DEFAULT nextval('public.flows_id_seq'::regclass);
ALTER TABLE ONLY public.groups ALTER COLUMN id SET DEFAULT nextval('public.groups_id_seq'::regclass);
ALTER TABLE ONLY public.pending_blob_deletions ALTER COLUMN id SET DEFAULT nextval('public.pending_blob_deletions_id_seq'::regclass);
ALTER TABLE ONLY public.permissions ALTER COLUMN id SET DEFAULT nextval('public.permissions_id_seq'::regclass);
ALTER TABLE ONLY public.projects ALTER COLUMN id SET DEFAULT nextval('public.projects_id_seq'::regclass);
ALTER TABLE ONLY public.runs ALTER COLUMN id SET DEFAULT nextval('public.runs_id_seq'::regclass);
ALTER TABLE ONLY public.saved_views ALTER COLUMN id SET DEFAULT nextval('public.saved_views_id_seq'::regclass);
ALTER TABLE ONLY public.uploads ALTER COLUMN id SET DEFAULT nextval('public.uploads_id_seq'::regclass);
ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);
ALTER TABLE ONLY public.ai_chat_turns
    ADD CONSTRAINT ai_chat_turns_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.ai_chats
    ADD CONSTRAINT ai_chats_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.ai_models
    ADD CONSTRAINT ai_models_pkey PRIMARY KEY (model_id);
ALTER TABLE ONLY public.ai_spend
    ADD CONSTRAINT ai_spend_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.banners
    ADD CONSTRAINT banners_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.connections
    ADD CONSTRAINT connections_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.data_contract_results
    ADD CONSTRAINT data_contract_results_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.data_contract_versions
    ADD CONSTRAINT data_contract_versions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.data_contracts
    ADD CONSTRAINT data_contracts_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.datasources
    ADD CONSTRAINT datasources_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.dbt_ai_texts
    ADD CONSTRAINT dbt_ai_texts_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.disabled_engines
    ADD CONSTRAINT disabled_engines_pkey PRIMARY KEY (engine_id);
ALTER TABLE ONLY public.flow_presence
    ADD CONSTRAINT flow_presence_pkey PRIMARY KEY (flow_id, instance);
ALTER TABLE ONLY public.flow_versions
    ADD CONSTRAINT flow_versions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.flows
    ADD CONSTRAINT flows_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.groups
    ADD CONSTRAINT groups_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.login_attempts
    ADD CONSTRAINT login_attempts_pkey PRIMARY KEY (ip, email);
ALTER TABLE ONLY public.pending_blob_deletions
    ADD CONSTRAINT pending_blob_deletions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.privacy_notice
    ADD CONSTRAINT privacy_notice_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.saved_views
    ADD CONSTRAINT saved_views_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.schema_state
    ADD CONSTRAINT schema_state_pkey PRIMARY KEY (key);
ALTER TABLE ONLY public.uploads
    ADD CONSTRAINT uploads_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.connections
    ADD CONSTRAINT uq_connection_project_name UNIQUE (project_id, name);
ALTER TABLE ONLY public.data_contracts
    ADD CONSTRAINT uq_contract_datasource UNIQUE (datasource_id);
ALTER TABLE ONLY public.data_contract_versions
    ADD CONSTRAINT uq_contract_version UNIQUE (datasource_id, version);
ALTER TABLE ONLY public.datasources
    ADD CONSTRAINT uq_datasource_project_name UNIQUE (project_id, name);
ALTER TABLE ONLY public.dbt_ai_texts
    ADD CONSTRAINT uq_dbt_ai_text UNIQUE (flow_id, flow_version, kind, key);
ALTER TABLE ONLY public.flow_versions
    ADD CONSTRAINT uq_flowversion UNIQUE (flow_id, version);
ALTER TABLE ONLY public.saved_views
    ADD CONSTRAINT uq_saved_view_project_name UNIQUE (project_id, name);
ALTER TABLE ONLY public.user_groups
    ADD CONSTRAINT user_groups_pkey PRIMARY KEY (user_id, group_id);
ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);
CREATE INDEX ix_ai_chat_turns_chat_id ON public.ai_chat_turns USING btree (chat_id);
CREATE INDEX ix_ai_chats_updated_at ON public.ai_chats USING btree (updated_at);
CREATE INDEX ix_ai_chats_user_id ON public.ai_chats USING btree (user_id);
CREATE INDEX ix_ai_spend_ts ON public.ai_spend USING btree (ts);
CREATE INDEX ix_ai_spend_turn_id ON public.ai_spend USING btree (turn_id);
CREATE INDEX ix_ai_spend_user_id ON public.ai_spend USING btree (user_id);
CREATE INDEX ix_audit_logs_action ON public.audit_logs USING btree (action);
CREATE INDEX ix_audit_logs_actor_id ON public.audit_logs USING btree (actor_id);
CREATE INDEX ix_audit_logs_target_type ON public.audit_logs USING btree (target_type);
CREATE INDEX ix_audit_logs_ts ON public.audit_logs USING btree (ts);
CREATE INDEX ix_banners_level ON public.banners USING btree (level);
CREATE INDEX ix_connections_project_id ON public.connections USING btree (project_id);
CREATE INDEX ix_contract_results_to_notify ON public.data_contract_results USING btree (id) WHERE ((notify)::text = 'pending'::text);
CREATE INDEX ix_data_contract_results_datasource_id ON public.data_contract_results USING btree (datasource_id);
CREATE INDEX ix_data_contract_results_evaluated_at ON public.data_contract_results USING btree (evaluated_at);
CREATE INDEX ix_data_contract_versions_datasource_id ON public.data_contract_versions USING btree (datasource_id);
CREATE INDEX ix_data_contracts_datasource_id ON public.data_contracts USING btree (datasource_id);
CREATE INDEX ix_datasources_next_refresh_at ON public.datasources USING btree (next_refresh_at);
CREATE INDEX ix_datasources_project_id ON public.datasources USING btree (project_id);
CREATE INDEX ix_dbt_ai_texts_flow_id ON public.dbt_ai_texts USING btree (flow_id);
CREATE INDEX ix_flow_presence_seen ON public.flow_presence USING btree (seen);
CREATE INDEX ix_flow_versions_flow_id ON public.flow_versions USING btree (flow_id);
CREATE INDEX ix_flows_next_run_at ON public.flows USING btree (next_run_at);
CREATE INDEX ix_flows_project_id ON public.flows USING btree (project_id);
CREATE UNIQUE INDEX ix_groups_name ON public.groups USING btree (name);
CREATE INDEX ix_pending_blob_deletions_delete_after ON public.pending_blob_deletions USING btree (delete_after);
CREATE INDEX ix_pending_blob_deletions_key ON public.pending_blob_deletions USING btree (key);
CREATE INDEX ix_permissions_group_id ON public.permissions USING btree (group_id);
CREATE INDEX ix_permissions_project_id ON public.permissions USING btree (project_id);
CREATE INDEX ix_permissions_user_id ON public.permissions USING btree (user_id);
CREATE INDEX ix_projects_parent_id ON public.projects USING btree (parent_id);
CREATE INDEX ix_runs_flow_id ON public.runs USING btree (flow_id);
CREATE INDEX ix_runs_kind ON public.runs USING btree (kind);
CREATE INDEX ix_runs_orchestration_queue ON public.runs USING btree (id) WHERE (((kind)::text = 'orchestration'::text) AND ((status)::text = 'PENDING'::text));
CREATE INDEX ix_runs_parent_run_id ON public.runs USING btree (parent_run_id);
CREATE INDEX ix_runs_task_id ON public.runs USING btree (task_id);
CREATE INDEX ix_saved_views_datasource_id ON public.saved_views USING btree (datasource_id);
CREATE INDEX ix_saved_views_project_id ON public.saved_views USING btree (project_id);
CREATE INDEX ix_uploads_dataset_id ON public.uploads USING btree (dataset_id);
CREATE INDEX ix_uploads_owner_id ON public.uploads USING btree (owner_id);
CREATE INDEX ix_uploads_parquet_key ON public.uploads USING btree (parquet_key);
CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);
CREATE INDEX ix_users_last_seen_at ON public.users USING btree (last_seen_at);
CREATE INDEX ix_users_oidc_issuer ON public.users USING btree (oidc_issuer);
CREATE INDEX ix_users_oidc_subject ON public.users USING btree (oidc_subject);
CREATE UNIQUE INDEX uq_runs_one_live_orchestration ON public.runs USING btree (flow_id) WHERE (((kind)::text = 'orchestration'::text) AND ((status)::text = 'STARTED'::text));
CREATE UNIQUE INDEX uq_users_oidc_identity ON public.users USING btree (oidc_issuer, oidc_subject) WHERE (oidc_subject IS NOT NULL);
ALTER TABLE ONLY public.ai_chat_turns
    ADD CONSTRAINT ai_chat_turns_chat_id_fkey FOREIGN KEY (chat_id) REFERENCES public.ai_chats(id);
ALTER TABLE ONLY public.ai_models
    ADD CONSTRAINT ai_models_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES public.users(id);
ALTER TABLE ONLY public.banners
    ADD CONSTRAINT banners_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.connections
    ADD CONSTRAINT connections_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id);
ALTER TABLE ONLY public.connections
    ADD CONSTRAINT connections_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);
ALTER TABLE ONLY public.datasources
    ADD CONSTRAINT datasources_connection_id_fkey FOREIGN KEY (connection_id) REFERENCES public.connections(id);
ALTER TABLE ONLY public.datasources
    ADD CONSTRAINT datasources_flow_id_fkey FOREIGN KEY (flow_id) REFERENCES public.flows(id);
ALTER TABLE ONLY public.datasources
    ADD CONSTRAINT datasources_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id);
ALTER TABLE ONLY public.datasources
    ADD CONSTRAINT datasources_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);
ALTER TABLE ONLY public.datasources
    ADD CONSTRAINT datasources_refresh_scheduled_by_fkey FOREIGN KEY (refresh_scheduled_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.disabled_engines
    ADD CONSTRAINT disabled_engines_disabled_by_fkey FOREIGN KEY (disabled_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.flow_versions
    ADD CONSTRAINT flow_versions_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.flow_versions
    ADD CONSTRAINT flow_versions_flow_id_fkey FOREIGN KEY (flow_id) REFERENCES public.flows(id);
ALTER TABLE ONLY public.flows
    ADD CONSTRAINT flows_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id);
ALTER TABLE ONLY public.flows
    ADD CONSTRAINT flows_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);
ALTER TABLE ONLY public.flows
    ADD CONSTRAINT flows_run_scheduled_by_fkey FOREIGN KEY (run_scheduled_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.groups(id);
ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);
ALTER TABLE ONLY public.permissions
    ADD CONSTRAINT permissions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);
ALTER TABLE ONLY public.privacy_notice
    ADD CONSTRAINT privacy_notice_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id);
ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.projects(id);
ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_datasource_id_fkey FOREIGN KEY (datasource_id) REFERENCES public.datasources(id);
ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_flow_id_fkey FOREIGN KEY (flow_id) REFERENCES public.flows(id);
ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_launched_by_fkey FOREIGN KEY (launched_by) REFERENCES public.users(id);
ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_parent_run_id_fkey FOREIGN KEY (parent_run_id) REFERENCES public.runs(id);
ALTER TABLE ONLY public.runs
    ADD CONSTRAINT runs_publish_project_id_fkey FOREIGN KEY (publish_project_id) REFERENCES public.projects(id);
ALTER TABLE ONLY public.saved_views
    ADD CONSTRAINT saved_views_datasource_id_fkey FOREIGN KEY (datasource_id) REFERENCES public.datasources(id);
ALTER TABLE ONLY public.saved_views
    ADD CONSTRAINT saved_views_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id);
ALTER TABLE ONLY public.saved_views
    ADD CONSTRAINT saved_views_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);
ALTER TABLE ONLY public.uploads
    ADD CONSTRAINT uploads_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id);
ALTER TABLE ONLY public.user_groups
    ADD CONSTRAINT user_groups_group_id_fkey FOREIGN KEY (group_id) REFERENCES public.groups(id);
ALTER TABLE ONLY public.user_groups
    ADD CONSTRAINT user_groups_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);
