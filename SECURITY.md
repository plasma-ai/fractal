# Security Policy

## Supported versions

Security fixes land in the current release of `plasma-fractal` (and its
`fractal` pointer distribution) on PyPI. Earlier releases receive no backported
fixes; upgrade to the current release to pick one up.

## Reporting a vulnerability

Please report security vulnerabilities **privately** — do not open a public
issue.

- **Preferred:** use GitHub's private vulnerability reporting on this repository
  (the **Security** tab -> **Report a vulnerability**).
- **Or:** email **eng@plasma.ai**.

We will acknowledge your report as quickly as we can and keep you updated on the
fix. Please give us a reasonable window to address the issue before any public
disclosure.

## Agent permissions

Nodes run their agent without permission prompts by design. An unattended loop
cannot stop to ask, so every seeded agent config disables its approval gate (the
warning at the top of the README names each agent's setting). A node can
therefore run any command its agent decides to run, with the operator's
credentials and the host's reach; the per-node `git worktree` isolates the
branch it commits to, not the filesystem or the network. This is the documented
operating model, not a misconfiguration to report: only launch nodes whose task
you would trust to run unsupervised, and prefer a sandboxed or otherwise
disposable host for anything else.

## Supply chain

Every GitHub Actions step under `.github/workflows/` is pinned to a full commit
SHA, with the matching release tag alongside, and Dependabot keeps those pins
current. The [HOL Plugin Scanner](https://github.com/hashgraph-online/hol-guard)
runs on every push and pull request (`scan.yaml`) and fails the build below a
score of 80 or on any high-severity finding.
