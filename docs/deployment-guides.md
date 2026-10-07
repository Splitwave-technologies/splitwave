# Deployment guides

General advice for putting SplitWave on common infrastructure. It is not a guarantee that a configuration will work in your environment (see the [support policy](https://split-wave.com/legal/support)). What is tested is stated in each section.

## What you need in any case
- A 64-bit Linux server (or a Kubernetes cluster) with at least 4 GB of RAM and 20 GB of disk.
- Outbound internet access during installation (the installer downloads k3s, Helm and the platform image).
- Optional: a domain name pointing to the server for HTTPS.
- A copy of the encryption key and of the database stored **outside** the server.

## Any VPS (Hetzner, DigitalOcean Droplet, Vultr, Linode, ps.kz and others)
**Tested:** Ubuntu 22.04 / 24.04 and Debian 12 with the one-command installer.
1. Create a server with one of those images, add your SSH key, open ports 22, 80 and 443 in the provider's firewall.
2. Connect over SSH, download the release archive, verify `SHA256SUMS`, unpack it and run the installer as described in [Installation](install.md).
3. Save the printed admin token and encryption key outside the server.

**DigitalOcean specifics:** a Droplet with 4 GB RAM or more works as a regular VPS. Use a Cloud Firewall for ports 22, 80, 443 and enable Droplet backups; they do not replace your own backups of the encryption key.

## Other Linux distributions
**Not tested.** The installer does not use `apt`; it needs `bash`, `curl`, `systemd` and a distribution that k3s supports (see the k3s requirements for Fedora, RHEL-compatible, SUSE and others). On systems with SELinux, install the k3s SELinux policy package first. If the installer stops with an error, install Kubernetes in the way your distribution recommends and use the Helm chart (below).

## An existing Kubernetes cluster (DigitalOcean Kubernetes, GKE, EKS, AKS, k3s, kubeadm)
**Tested:** k3s. Other clusters follow the same Helm chart, but we have not tested them.
1. Make sure you have `kubectl` and Helm access to the cluster and an ingress controller.
2. Unpack the release archive and install the chart from `deploy/helm/splitwave` with your values (domain, storage class, admin token secret).
3. Make sure the platform can build and push images to a registry that your cluster can pull from (a managed registry such as DigitalOcean Container Registry works in principle).
4. Back up the database and the encryption key.

## After the installation
- Put the panel behind HTTPS ([Domains and TLS](domains-tls.md)).
- Enable two-factor authentication on the admin account of the site if you use paid plans.
- Plan updates: only the latest release is supported.
