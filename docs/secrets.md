# Secrets

Secrets are values the application needs but the repository must not contain: database passwords, API keys, tokens.

## The rules

- Values are **encrypted** in the database and leave the system **only** into a Kubernetes Secret at deploy time. No API call, log or audit record contains a value.
- After saving, a value is never shown again. You can replace it, restore an older version or delete it.
- Every change creates a new **version**. You can list versions and restore one.
- A developer sees the **names** of secrets but not their values and cannot change them. Writing secrets needs the `devops` or `admin` role.

## Scope

A secret applies to **all environments** of a project or to **one environment**. An environment-level value overrides the project-wide one with the same name.

## Delivery to the application

At each deploy the platform creates or updates the Kubernetes Secret of the environment and connects it to the Deployment through `envFrom`. A hash annotation on the pod template restarts the pods when a value changes. *Sync secrets* (`POST /api/projects/{slug}/secrets/sync`) delivers them without a deploy.

## Expiry and rotation

For each secret you can set an expiry date and a rotation period in days. The platform reminds you through notifications (*secret expiring soon*, *secret expired*, *rotation due*). It does not block anything on its own.

## Import from `.env`

On the project page (*Import from .env*) and in the last step of the wizard you can turn a `.env` file into secrets.

Sources: a file from your computer, pasted text, or a file from the project's repository (path and branch can be set; by default `.env` on the environment's branch). For a private repository the project's stored token is used, or one you enter (used once, not stored).

- A **preview** comes first. It shows names and the action (create, update, unchanged, exists), never values.
- Existing secrets are **not overwritten** unless you tick *overwrite*. The same value does not create a new version.
- Choose one environment or all; you can apply only selected variables.
- Normal dotenv syntax is supported: comments, `export`, quoted values (`\n`, `\t`, `\"`, `\\` and line breaks work in double quotes), a comment after the value. `${VAR}` substitutions are not expanded. Empty values and invalid lines are skipped and listed.
- Limits: 256 KB per file, 512 variables, 64 KB per value.

Keeping a `.env` file in the repository is unsafe: everyone with access sees it. After the import remove the file and change the passwords.

API: `POST /api/projects/{slug}/secrets/import[?environment=prod]` with `{"content":"...","overwrite":false,"dry_run":true,"keys":["A","B"]}` or `{"from_repo":true,"path":".env","ref":"main","token":"..."}`; needs `secrets:write`.

## Application logs hide values

Pod logs shown in the console replace secret values with `***`.
