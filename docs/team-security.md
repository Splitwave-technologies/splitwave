# Team and security

## Roles

| Role | Can |
|---|---|
| `admin` | everything: create and delete projects, environments, domains, users, tokens, licence |
| `devops` | deploy and roll back, write secrets, read audit and licence |
| `developer` | read, deploy, read pod logs, see secret **names** (not values) |
| `viewer` | read only |
| `none` | no global rights: only the projects where the user is a member |

Only an administrator creates projects and binds domains. A user name is Latin letters, digits and `. _ @ + -`, so an email address works (it is lower-cased).

### Project members

Besides the global role, add people to a single project with a role of their own (`devops`, `developer`, `viewer`) under *Project members*. A user with global role `none` sees only those projects.

## Users and sessions

*Users* (administrator): create, change role, delete. A browser session lasts at most 12 hours (`auth.sessionTtlHours`) and ends after 60 minutes without activity (`auth.sessionIdleMinutes`). After 5 failed sign-ins for one name within 15 minutes the name is locked for a while (`auth.loginMaxFailures`). You can list and revoke your own sessions in *Account*.

## Two-factor authentication

Each user can enable TOTP (an authenticator app) in *Account*; recovery codes are shown once. An administrator can require 2FA for everyone (Helm `auth.require2fa=true`): a user without it can only set it up after signing in.

## API tokens

*Tokens* (administrator). Choose the **minimum role**, copy the secret (shown once), and use `Authorization: Bearer ...`. Only a hash is stored. Revoke at any time. API tokens are not user accounts and do not count towards the user limit.

## Single sign-on (Pro, Max)

OpenID Connect with Authorization Code and PKCE. Configure with `sso.*` in the Helm values: `issuer`, `clientId`, `clientSecret`, and `redirectUri` (`https://<host>/api/sso/callback`, the same as in your identity provider). `roleMap` maps groups to roles (`{"platform-admins":"admin"}`), `allowedDomains` restricts email domains, `defaultRole` applies when no group matches (default `none`). For a provider inside your network (for example Keycloak in the cluster) set `allowPrivate`. Accounts are matched by issuer and subject, never by email: an equal email with a local user does not grant access.

## Audit log

Every important action is recorded: who, what, when. Records form a **hash chain**: each carries a hash of the previous one, so a changed or deleted record is detected. *Audit, Verify chain* checks the whole chain and shows the head hash. Write the head hash down somewhere outside the platform: later it proves that history was not rewritten.

- *Explorer* searches events with a query language and shows a histogram: see [API](api.md).
- *Audit export* (Lite and above) downloads events as CSV.
- Send events to a SIEM (Pro and above): see [Notifications and SIEM](notifications-siem.md).

## Explorer queries

Type conditions separated by spaces; all must match: `action:deploy* project:shop`, `result:failed -actor:ci-bot`, `"secret_write" actor:alice`. Fields: `action`, `actor`, `project`, `result` (`ok` or `failed`). `*` is a wildcard, `-` excludes, quotes keep spaces. `OR` is not supported. Click a value in the right panel to add it to the query.
