# Security policy

SplitWave handles deployment credentials, so we take security reports seriously.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Use GitHub's private vulnerability reporting (*Security*, *Report a vulnerability*) or write to **support@split-wave.com** with the subject "Security".

Please include: the version, what you found, how to reproduce it and what you think the impact is. We read security reports with priority and handle them on a best-effort basis (this is a small team, there is no guaranteed response time); we will keep you informed while we work on a fix and credit you in the release notes if you wish. Please allow us reasonable time to release a fix before you disclose the problem publicly.

## Supported versions

The latest release receives security fixes. The project is in active development; review it before you run production workloads (see the status section of the README).

## What is in scope

The control plane, the web console, the Helm chart and the agent in this repository. Out of scope: findings that need an already compromised administrator account, social engineering, and denial of service by volume.
