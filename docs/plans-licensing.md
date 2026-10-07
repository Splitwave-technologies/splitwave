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

## What is in the free core and what is paid

The free core runs **one project with one environment** and **one built-in administrator account**, with every deploy feature (wizard, builds, deploy, rollback, secrets, approvals, 2FA, audit view, notifications). API tokens (for CI) have their own roles and are not user accounts.

Paid modules come in the paid image and switch on with a valid key:

- **Teams**: several user accounts, roles (DevOps, developer, viewer) and project members.
- **Environments**: several environments per project and deploys by branch.
- **Projects**: several projects on one platform.
- **SSO, SIEM export, remote clusters, servers with an agent, PR previews, audit export** (by plan, see the table).

A plan sets how many of each you may create. Limits apply to **creating**; existing data is never deleted. Short-lived previews do not count as environments.

## When a licence ends

Nothing is deleted, but the installation continues as Community:

| | What keeps working | What stops |
|---|---|---|
| Accounts | Administrator accounts | Accounts with other roles and project members have no access until a key is installed again (their data stays) |
| Projects | All projects stay visible and can be deployed from the console | New projects cannot be created; only the first project deploys automatically on a push |
| Environments | All environments stay visible and can be deployed from the console | New environments cannot be created; only the first environment of a project deploys automatically on a push |
| Paid modules | | SSO, SIEM, remote clusters, servers, previews and audit export switch off |
| API tokens | Keep working with their roles | |

Install a new key and everything returns without any other change.

## How a licence key works

- The key is a signed string. The platform verifies it **offline** with a public key built into the product; it never contacts a licence server and sends no usage data. The separate update notice only reads a public file with the latest version (`UPDATE_CHECK=false` turns it off).
- A key can be **bound to one installation** by its *Installation ID* (shown in the console under *License and plans*, looks like `dsp-` and 20 characters). A bound key does not work elsewhere.
- A key is valid for the paid period plus one day. A plan is a subscription that renews automatically; each renewal sends a **new key** for the same installation by email and in your account. Install it before the old one expires.
- Cancel any time in your account (*Manage subscription*): charging stops, the paid period stays valid.
- When a key expires or is removed, the paid modules switch off and the installation continues as Community (see the table above). No data is lost.

## Getting a key

1. Create an account on the site and sign in.
2. Turn on **two-factor authentication** in your account: buying a plan is not possible without it.
3. *Pricing*, choose a plan and period, pay.
4. In *Orders and keys* enter the **Installation ID** from your console. It cannot be changed afterwards, because the key is bound to it.
5. The key is signed (usually within a few hours) and appears in your account and in an email.
6. Install it: with Helm `--set secrets.licenseKey=<key>` (or `--license-key` for the quick start) and the Pro image. The *License and plans* page then shows the plan, status and usage.

## Refunds

Within 14 days of payment, if the key has not been put into use: write to support. See the [public offer](https://split-wave.com/legal/offer).
