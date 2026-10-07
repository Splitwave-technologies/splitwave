# Quick start

From a clean server to a first deployed application. Plan for about 20 minutes.

## What you need

- A server with **Ubuntu 22.04/24.04 or Debian 12**, at least **4 GB RAM** and **20 GB disk**, internet access and root.
- A Git repository with an application. A Dockerfile helps but is not required.
- Optional: a domain name pointing at the server (an A record), for HTTPS.

The platform with its database and registry uses about 1.3 GB of RAM and 4 GB of disk; your applications run on the same server.

## 1. Install

Run the installer as root. It installs k3s (Kubernetes), Helm, a local image registry and the platform. A typical run takes about 5 minutes and can be repeated safely.

```bash
sudo ./deploy/quickstart/install-single-node.sh \
  --chart ./deploy/helm/devsecops-platform \
  --image <registry>/control-plane:<version> \
  --host panel.example.com --email you@example.com
```

Without `--host` the panel is served at `<ip>.sslip.io`, which works without your own DNS. With `--host`, create the A record first so Let's Encrypt can issue a certificate (ports 80 and 443 must be open).

When it finishes the script prints:
- the address of the panel;
- the path of the administrator token (`/root/dsp-admin-token`);
- the path of the **secrets encryption key**.

**Copy the encryption key to a safe place now.** Without it the secrets stored in the database cannot be read. Details and options: [Installation](install.md).

## 2. Sign in

Open the address, choose *Use token* and paste the administrator token. You see the empty console.

## 3. Connect a repository

*Projects, Connect repository.* Paste the link, review the proposed settings, choose where to deploy and confirm. The platform creates the project and the first environment and starts the first build. Details: [Connecting a project](connect-project.md).

## 4. Add the webhook

The last step of the wizard shows the webhook address, secret and events. Add them in your repository settings (or press the button that creates the webhook for you when the platform has a token for the repository). From now on a push to the environment's branch deploys automatically.

## 5. Add secrets

If the application needs a database password or API keys, open the project, *Secrets*, and add them (or import a `.env` file). See [Secrets](secrets.md). They reach the application at the next deploy, or at once with *Sync secrets*; pods restart automatically when a value changes.

## 6. Check

The project page shows the current image, pods and their restarts, builds with logs and the release history. If something fails, the failure reason is shown on the release; see [Troubleshooting](troubleshooting.md).

## Next steps

- Bind your own domain: [Domains and certificates](domains-tls.md).
- Add colleagues and roles: [Team and security](team-security.md).
- Get notified about failures: [Notifications and SIEM](notifications-siem.md).
