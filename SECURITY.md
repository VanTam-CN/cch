# Security

The initial public line is `0.5.0rc1`. Fixes are maintained on `main`; security
support for old snapshots is not promised.

## Reporting a vulnerability

Use this repository's **Security → Report a vulnerability** flow for a private
report. If the private report option is unavailable, open an issue asking for a
private reporting channel without including sensitive details.

Describe the affected version, prerequisites, a synthetic reproduction, impact,
and a proposed mitigation if available. Never attach credentials or private data.
Response times are not guaranteed for this independently maintained project.

## Boundaries

- Approval records and reviewer names are audit data, not authentication.
- The current permission model is cooperative. Processes running as the same
  OS user are not separated by CCH. Use host permissions for stronger isolation.
- The web interface binds to loopback and is intended for local use. It is not
  an authenticated shared service.
- Model results and imported documents are untrusted inputs.
- Optional WPS preview conversion uploads documents only when explicitly enabled.
- Optional Hindsight calls are remote operations; memory is never fact authority.
- Runtime stores can contain private documents, previews, proposals, and history.
  Keep them outside this public repository and back them up according to your needs.
- Contract validation is not a comprehensive secret or semantic detector.

See [GOVERNANCE.md](GOVERNANCE.md) for publication and evidence invariants.
