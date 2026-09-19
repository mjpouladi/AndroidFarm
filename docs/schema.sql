-- طرح PostgreSQL برای backend آینده؛ migration اجراشده نیست.
BEGIN;
CREATE TABLE farm_users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  oidc_subject text NOT NULL UNIQUE,
  display_name text NOT NULL,
  role text NOT NULL CHECK (role IN ('viewer','operator','admin')),
  disabled_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE hosts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  agent_identity text NOT NULL UNIQUE,
  max_active smallint NOT NULL DEFAULT 10 CHECK (max_active BETWEEN 1 AND 10),
  last_heartbeat timestamptz,
  generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0)
);
CREATE TABLE proxies (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  label text NOT NULL,
  type text NOT NULL CHECK (type IN ('socks','http','wireguard','openvpn')),
  endpoint inet,
  endpoint_port integer CHECK (endpoint_port BETWEEN 1 AND 65535),
  secret_ref text NOT NULL UNIQUE,
  country_code char(2),
  expected_egress inet,
  observed_egress inet,
  sticky_expires_at timestamptz,
  health text NOT NULL DEFAULT 'unknown' CHECK (health IN ('unknown','healthy','unhealthy')),
  checked_at timestamptz
);
CREATE TABLE devices (
  id text PRIMARY KEY CHECK (id ~ '^num(0[1-9]|[1-9][0-9]|1[0-9]{2}|200)$'),
  host_id uuid NOT NULL REFERENCES hosts(id),
  alias varchar(80) NOT NULL,
  proxy_id uuid UNIQUE REFERENCES proxies(id),
  volume_name text NOT NULL UNIQUE,
  serial text NOT NULL UNIQUE,
  image_digest text NOT NULL,
  state text NOT NULL DEFAULT 'provisioning' CHECK (state IN
    ('provisioning','stopped','queued','starting','verifying','ready','stopping','backing_up','restoring','unknown','error','retired')),
  generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
  observed_at timestamptz,
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  retired_at timestamptz,
  UNIQUE (id, host_id)
);
CREATE TABLE device_phones (
  device_id text PRIMARY KEY REFERENCES devices(id),
  phone_ciphertext bytea NOT NULL,
  phone_hmac bytea NOT NULL UNIQUE CHECK (octet_length(phone_hmac) = 32),
  last_four char(4) NOT NULL CHECK (last_four ~ '^[0-9]{4}$'),
  encryption_key_version text NOT NULL,
  hmac_key_version text NOT NULL,
  assigned_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE device_grants (
  user_id uuid NOT NULL REFERENCES farm_users(id),
  device_id text NOT NULL REFERENCES devices(id),
  can_operate boolean NOT NULL DEFAULT false,
  PRIMARY KEY (user_id, device_id)
);
CREATE TABLE sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id text NOT NULL,
  host_id uuid NOT NULL,
  owner_id uuid NOT NULL REFERENCES farm_users(id),
  state text NOT NULL CHECK (state IN ('queued','starting','verifying','ready','stopping','unknown','completed','cancelled','failed')),
  requested_at timestamptz NOT NULL DEFAULT now(),
  ready_at timestamptz,
  lease_until timestamptz,
  ended_at timestamptz,
  version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
  FOREIGN KEY (device_id, host_id) REFERENCES devices(id, host_id),
  UNIQUE (id, host_id)
);
CREATE UNIQUE INDEX one_open_session_per_device ON sessions(device_id)
  WHERE state IN ('queued','starting','verifying','ready','stopping','unknown');
CREATE TABLE slots (
  host_id uuid NOT NULL REFERENCES hosts(id),
  slot_no smallint NOT NULL CHECK (slot_no BETWEEN 1 AND 10),
  session_id uuid UNIQUE,
  reserved_at timestamptz,
  fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
  PRIMARY KEY(host_id, slot_no),
  FOREIGN KEY (session_id, host_id) REFERENCES sessions(id, host_id),
  CHECK ((session_id IS NULL) = (reserved_at IS NULL))
);
-- Worker باید max_active میزبان را علاوه بر slot_no و count واقعی host رعایت کند.
CREATE TABLE jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_id uuid NOT NULL REFERENCES farm_users(id),
  device_id text REFERENCES devices(id),
  session_id uuid REFERENCES sessions(id),
  operation text NOT NULL CHECK (operation IN ('provision','start','stop','backup','restore','proxy_check','reconcile')),
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','claimed','running','succeeded','failed','cancelled','unknown')),
  idempotency_key uuid NOT NULL,
  request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  worker_id text,
  lease_until timestamptz,
  not_before timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  error_code text,
  UNIQUE (actor_id, idempotency_key)
);
CREATE INDEX claimable_jobs ON jobs(not_before, created_at) WHERE status = 'queued';
CREATE UNIQUE INDEX one_executing_job_per_device ON jobs(device_id)
  WHERE status IN ('claimed','running','unknown') AND operation <> 'reconcile';
CREATE TABLE backups (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id text NOT NULL REFERENCES devices(id),
  job_id uuid NOT NULL UNIQUE REFERENCES jobs(id),
  storage_ref text NOT NULL,
  sha256 char(64) CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes bigint CHECK (size_bytes >= 0),
  manifest jsonb NOT NULL,
  state text NOT NULL CHECK (state IN ('pending','verified','failed','expired')),
  created_at timestamptz NOT NULL DEFAULT now(),
  verified_at timestamptz,
  CHECK (state <> 'verified' OR (sha256 IS NOT NULL AND verified_at IS NOT NULL))
);
CREATE TABLE audit_events (
  sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor_id uuid REFERENCES farm_users(id),
  action text NOT NULL,
  device_id text REFERENCES devices(id),
  job_id uuid REFERENCES jobs(id),
  request_id uuid NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('accepted','succeeded','failed','denied')),
  safe_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  happened_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_by_device ON audit_events(device_id, sequence DESC);
-- نقش‌های API/worker باید بدون UPDATE/DELETE روی audit_events تعریف شوند.
-- encrypted values، credentials، OTP و command output خام وارد safe_metadata نشوند.
COMMIT;
