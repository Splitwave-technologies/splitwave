# Domains and certificates

## Bind a domain

1. At your registrar create an A record (or one wildcard record such as `*.apps.example.com`) pointing at the server.
2. In the project, *Environments*, press **Domain** (administrator). The platform creates an Ingress; with HTTPS enabled it gets a certificate and redirects HTTP to HTTPS. An empty field or *Remove domain* removes the binding. You can also enter a domain in the wizard.
3. The platform needs the right to manage Ingress in the application's namespace: `provisioner.enabled` (on in the quick start) or `rbac.manageIngress=true`.

In an existing cluster set `onboarding.tls=auto|custom`, `onboarding.certResolver` (the ACME resolver name in Traefik, default `le`), `onboarding.tlsSecret` and `onboarding.ingressAnnotations` (JSON, for example `{"cert-manager.io/cluster-issuer":"letsencrypt"}` for nginx with cert-manager).

If your DNS provider has a CDN or proxy (for example the orange cloud at Cloudflare), switch the record to *DNS only* while the certificate is issued. New records may take up to an hour to appear on your own computer; check with `dig +short name @1.1.1.1`.

## Your own certificate

For a corporate CA or a purchased wildcard: the **Domain** window, block **Own certificate** (or `PUT /api/projects/{slug}/environments/{env}/domain/certificate`).

1. Bind the domain first.
2. Upload the PEM chain (**server certificate first, then intermediates**) and the private key **without a password** (remove it with `openssl pkey -in key.pem -out key-nopass.pem`).
3. The platform checks that the key matches the certificate, that the certificate covers the domain (a wildcard covers one level), the validity period, the key type (RSA from 2048 bits, ECDSA P-256/384/521) and the chain order. It warns about missing intermediates, wrong order, self-signed certificates and expiry within 30 days.
4. The key is written **only to the cluster** as the Secret `<deployment>-tls`. It is not in the platform database, API responses or the audit log (which records the domain, the SHA-256 fingerprint and the validity). A Secret of that name that the platform did not create is never overwritten (error 409).
5. The application's Ingress switches to that Secret.

To renew, upload the new certificate the same way. Binding the same domain again keeps the certificate; changing or removing the domain deletes the old key from the cluster. **Return the platform certificate** goes back to the automatic one and deletes your key.
