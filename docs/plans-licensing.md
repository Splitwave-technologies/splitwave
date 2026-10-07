# Plans and licences

## Plans

| | Community | Lite | Pro | Max |
|---|---|---|---|---|
| Price | free | $20 per month or $200 per year | $100 per month or $1000 per year | $200 per month or $2000 per year |
| Projects | 1 | 5 | 25 | unlimited |
| Environments per project | 1 | 3 | 10 | unlimited |
| Users | 1 | 3 | 10 | unlimited |
| Remote clusters | no | no | 3 | unlimited |
| Servers with an agent | no | no | 5 | unlimited |
| PR previews at a time | no | 3 | 20 | unlimited |
| Audit export | no | yes | yes | yes |
| SSO, SIEM, several clusters, servers | no | no | yes | yes |
| Support | bug reports | bug reports | bug reports | bug reports |

A paid plan raises the limits (projects, environments, users, clusters, servers, previews) and adds the paid modules. It adds no service guarantee, no live support and no extra warranty: the software is used at the customer's own responsibility, as described in the [public offer](https://split-wave.com/legal/offer) and the [support policy](https://split-wave.com/legal/support).

Everything in the free core (wizard, builds, deploy, rollback, secrets, `.env` import, approvals, 2FA, audit view, notifications, connection check) is in every plan.

Limits apply to **creating** projects, environments and users. Existing ones are never deleted or locked. API tokens are not users; short-lived previews do not count as environments.

## How a licence key works

- The key is a signed string. The platform verifies it **offline** with a public key built into the product; it never contacts a licence server and sends no usage data. The separate update notice only reads a public file with the latest version (`UPDATE_CHECK=false` turns it off).
- A key can be **bound to one installation** by its *Installation ID* (shown in the console under *License and plans*, looks like `dsp-` and 20 characters). A bound key does not work elsewhere.
- A key is valid for the paid period plus one day. A plan is a subscription that renews automatically; each renewal sends a **new key** for the same installation by email and in your account. Install it before the old one expires.
- Cancel any time in your account (*Manage subscription*): charging stops, the paid period stays valid.
- When a key expires or is removed, the paid modules switch off and the installation continues as Community. No data is lost.

## Getting a key

1. Create an account on the site and sign in.
2. Turn on **two-factor authentication** in your account: buying a plan is not possible without it.
3. *Pricing*, choose a plan and period, pay.
4. In *Orders and keys* enter the **Installation ID** from your console. It cannot be changed afterwards, because the key is bound to it.
5. The key is signed (usually within a few hours) and appears in your account and in an email.
6. Install it: with Helm `--set secrets.licenseKey=<key>` (or `--license-key` for the quick start) and the Pro image. The *License and plans* page then shows the plan, status and usage.

## Refunds

Within 14 days of payment, if the key has not been put into use: write to support. See the [public offer](https://split-wave.com/legal/offer).
