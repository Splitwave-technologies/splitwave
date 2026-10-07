# Notifications and SIEM

## Notifications

*Notifications* sends selected audit events to a channel. Types:

- **Webhook**: JSON `POST` to your address; with a secret the request is signed (HMAC).
- **Slack-compatible webhook**: Slack, Mattermost and similar.
- **Telegram**: a bot token and a chat id.

For each channel choose the events. Defaults are the ones you rarely want to miss.

| Event | On by default |
|---|---|
| Deploy failed, Rollback executed | yes |
| Approval requested, granted, denied, action failed | yes |
| Break-glass used | yes |
| Secret expiring soon, expired, rotation due | yes |
| Audit log integrity broken | yes |
| Deploy started, access denied, secret changed, restored or deleted, user, project created or deleted, approval executed or expired | no |

*Send test* checks the channel. Outgoing addresses are checked like in the repository wizard (public addresses only).

## SIEM export (Pro, Max)

*SIEM* streams the audit log to your security system:

- **syslog** (RFC 5424) over TCP, UDP or TLS, format **CEF** or JSON;
- **Splunk HEC**;
- any **HTTP endpoint** (NDJSON).

Severity follows the CEF scale 0 to 10. Add several destinations, test each one and delete what you no longer need. Settings `siem.*` in the Helm values.
