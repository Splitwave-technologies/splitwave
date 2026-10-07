# External services: databases, cache, storage

The platform deploys your application but does **not** create its database (Postgres, MySQL, Mongo), S3 or MinIO storage, Redis or queues. Run those next to the application (in the same cluster or at a provider) and put their addresses and passwords into the project's [secrets](secrets.md).

The wizard reads the variable names in `.env.example` and warns about which services the application probably needs. Without them the application starts and crashes: the reason is in the pod log (*Logs* in *State in the cluster*; for a crashed pod the log of the previous run opens, with secret values hidden).

## Connection check

If the database, cache or storage is on a separate host, use **Check connections** on the environment (or `POST /api/projects/{slug}/connections/probe`, needs `deploy`). The platform walks through **DNS, TCP, TLS and login** from the server where it runs and explains what is wrong.

| You see | It means | What to do |
|---|---|---|
| port closed (`refused`) | the service is down or listens elsewhere | start it; listen on more than 127.0.0.1 |
| no answer (`timeout`) | usually a firewall or security group | allow the IP the platform connects from |
| certificate expired or not yet valid | validity period | reissue |
| name does not match the certificate | the answer lists the names in it | connect by one of them or issue a certificate with the right SAN |
| unknown certificate authority | your own CA | paste the CA (PEM) in *Own certificate authority* |
| certificate expires in N days | warning (under 30 days) | reissue in advance |
| login refused, no such database, `pg_hba.conf` | login | check credentials and database name; allow the platform's IP in `pg_hba.conf` |

Supported: PostgreSQL (the login is really checked), Redis (AUTH and PING), S3 and MinIO (a SigV4-signed request), HTTPS (status code), MySQL (TLS and greeting; login is not checked yet) and any TCP port. TLS modes: off, if supported, required (encryption without certificate check, with a warning), verify CA, verify CA and name.

Security: credentials you type are used in memory only and appear in no response or log (the log records service type, address, TLS mode and outcome). The check refuses loopback, link-local (including 169.254.169.254) and special addresses, connects to the already-resolved IP, and allows at most 10 targets per request and 30 checks per minute per user. It runs from the platform's server, so the network rules of the application's pod can differ.

## The application's logs

*State in the cluster* has **Logs** for every pod (role developer and above): the last 100 to 2000 lines; for a crashed pod, the previous run. Typical causes right after a deploy: missing environment variables (project secrets), no access to the database or storage, a wrong port.
