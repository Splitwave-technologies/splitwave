# Platform API

Everything the web console does goes through this HTTP API; you can script all of it.
The interactive reference (every endpoint, every field, try-it-out) is served by your installation at **`/docs`** (Swagger UI), and the raw schema at **`/openapi.json`**.
This page is the short guide to the platform API of your own installation: authentication, conventions, and the calls people use most.

Examples use `$BASE` (for example `https://dsp.example.com`) and `$TOKEN` (an API token).

## Authentication

Send `Authorization: Bearer $TOKEN`. Tokens are created in the console (**Tokens**) or with `POST /api/tokens`; the secret is shown **once**.

| Role | What it can do |
|---|---|
| `admin` | everything |
| `devops` | read, deploy, roll back, manage secrets, read audit and license |
| `developer` | read, deploy, list secret **names** (never values) |
| `viewer` | read only |

Create a token for CI (admin token required):

```bash
curl -s -X POST $BASE/api/tokens -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
     -d '{"name":"ci-shop","role":"developer"}'
# {"id":"…","name":"ci-shop","role":"developer","token":"…"}   <- store it now
```

Console users sign in with a username and password (`POST /api/auth/login`, optional TOTP). Browser sessions are separate from API tokens.

## Conventions

- JSON in, JSON out. Names (`slug`, namespace, deployment) are lowercase DNS names.
- Errors: `401` no or bad credentials, `403` your role lacks the permission **or the plan limit is reached**, `404` not found, `409` conflict, `422` invalid input (the `detail` field says what is wrong).
- Secret values are write-only: no endpoint returns them.
- Every change is written to the audit log (`GET /api/audit`).

## Deploy and roll back

```bash
# build from the branch and roll out (needs `deploy`)
curl -s -X POST "$BASE/api/projects/shop/redeploy?environment=prod" -H "Authorization: Bearer $TOKEN"

# releases of a project, newest first
curl -s "$BASE/api/projects/shop/releases?environment=prod" -H "Authorization: Bearer $TOKEN"

# roll back without a rebuild (needs `rollback`); `to=<release id>` picks a specific release, otherwise the previous one
curl -s -X POST "$BASE/api/projects/shop/rollback?environment=prod" -H "Authorization: Bearer $TOKEN"

# state in the cluster: replicas, pods, conditions
curl -s "$BASE/api/projects/shop/runtime?environment=prod" -H "Authorization: Bearer $TOKEN"
```

`environment` is a query parameter (default `prod`). A push to the connected branch deploys automatically through the Git webhook (`POST /webhook/{provider}/{slug}`); see `GET /api/projects/{slug}/webhook` for the URL and how to register it.
If the new version does not become ready, the rollout is reported as failed and the previous version stays in place.

## Projects and environments

| Call | Purpose |
|---|---|
| `GET /api/projects`, `POST /api/projects` | list / create (with its environments) |
| `PATCH` / `DELETE /api/projects/{slug}` | change / delete |
| `GET`, `POST /api/projects/{slug}/environments` | environments |
| `PATCH`, `DELETE /api/projects/{slug}/environments/{name}` | `branch`, `auto_deploy`, `require_approval`, `cluster`, `server` |
| `GET`, `PUT`, `DELETE /api/projects/{slug}/members[/{username}]` | project members and roles |

Create a project:

```bash
curl -s -X POST $BASE/api/projects -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' -d '{
  "slug":"shop","repo_full_name":"acme/shop",
  "environments":[{"name":"prod","namespace":"shop","deployment_name":"shop","container_name":"shop"}]}'
```

## Secrets

Values are encrypted at rest, versioned, and never returned.

```bash
curl -s -X PUT $BASE/api/projects/shop/secrets/DATABASE_URL -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"value":"postgres://…","rotation_days":90}'
curl -s $BASE/api/projects/shop/secrets -H "Authorization: Bearer $TOKEN"      # names, versions, expiry only
curl -s -X POST $BASE/api/projects/shop/secrets/sync -H "Authorization: Bearer $TOKEN"   # push to the cluster
```

`POST …/secrets/import` accepts the text of a `.env` file. `GET …/secrets/{key}/versions` and `POST …/secrets/{key}/restore` give history and restore.

## Custom domain and certificate

```bash
curl -s -X PUT $BASE/api/projects/shop/environments/prod/domain -H "Authorization: Bearer $ADMIN" \
     -H 'Content-Type: application/json' -d '{"host":"shop.example.com"}'      # HTTPS via Let's Encrypt

# your own certificate (PEM chain: server certificate first; unencrypted private key)
curl -s -X PUT $BASE/api/projects/shop/environments/prod/domain/certificate -H "Authorization: Bearer $ADMIN" \
     -H 'Content-Type: application/json' \
     -d "$(jq -n --rawfile c fullchain.pem --rawfile k privkey.pem '{certificate:$c,private_key:$k}')"
```

The certificate is validated (key matches, host name covered, not expired). `DELETE …/domain/certificate` returns to automatic certificates.

## Connection check (external database, cache, storage)

From the platform's own network, with step-by-step results (DNS, TCP, TLS, login):

```bash
curl -s -X POST $BASE/api/projects/shop/connections/probe -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"targets":[{"kind":"postgres","host":"db.internal","port":5432,"tls":"verify-full"}]}'
```

`kind`: `postgres`, `mysql`, `redis`, `s3`, `https`, `tcp`. `tls`: `off`, `prefer`, `require`, `verify-ca`, `verify-full`; `ca_pem` may carry your CA.
Credentials, if given, are used for this one check and are not stored. Private and metadata addresses are refused, and checks are rate-limited.

## Event Explorer

`GET /api/explorer/events` (needs `audit:read`). Parameters: `q`, `range` (`1h`, `24h`, `7d`, `30d`, `90d`) or `start`/`end` (ISO time, up to 366 days), `limit` (≤ 200), `offset`.

Query language: space-separated conditions, all must match.

| Syntax | Meaning |
|---|---|
| `action:deploy*` | field equals value; `*` is a wildcard (case-insensitive) |
| `-actor:ci-bot` | exclude |
| `result:failed` | `ok` or `failed` (actions containing failed, denied, error, rejected, forbidden) |
| `timeout` | free text in action, actor and details |
| `"two words"` | quotes for values with spaces |

Fields: `action`, `actor`, `project`, `result`. `OR` is not supported. A bad query returns `422` with the reason.
The response has `total`, the page of `events`, a `histogram` (buckets with `n` and `failed`) and `fields` (top values with counts).

```bash
curl -s -G $BASE/api/explorer/events -H "Authorization: Bearer $TOKEN" \
     --data-urlencode 'q=project:shop result:failed' --data-urlencode 'range=7d'
```

## Audit and integrity

`GET /api/audit?project=&action=&limit=` lists events; `GET /api/audit/verify` checks the tamper-evident chain and returns the head hash. Record the head hash somewhere outside the platform to prove later that history was not rewritten.

## Approvals

Environments with `require_approval` queue deployments: `GET /api/approvals`, then `POST /api/approvals/{id}/approve` or `/deny`. The author cannot approve their own request.

## License and plan

`GET /api/license` returns the plan, limits, features and the **installation ID** to quote when ordering a key. Limits are enforced on creation: you get `403` with the limit named when a plan maximum is reached.

| Plan | Projects | Environments / project | Users |
|---|---|---|---|
| Community | 1 | 1 | 1 |
| Lite | 5 | 3 | 3 |
| Pro | 25 | 10 | 10 |
| Max | unlimited | unlimited | unlimited |

Other endpoints, grouped: notifications (`/api/notifications/*`), SIEM destinations (`/api/siem/*`), clusters (`/api/clusters`), servers (`/api/servers`), previews (`/api/projects/{slug}/previews`), users (`/api/users`), onboarding wizard (`/api/onboarding/*`), overview figures (`/api/overview`), health (`/health`). Clusters, servers, SIEM and previews come with the paid module.
