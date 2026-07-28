# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities through GitHub's private vulnerability reporting
("Report a vulnerability" on the Security tab) rather than a public issue.

Please include what an attacker can achieve, the steps to reproduce, and the affected
version. An initial response should be expected within a week.

## Scope

In scope:

- Credential exposure — leaking a bearer token or the Codex session into arguments,
  output, logs, files, or a host other than the configured endpoint.
- Bypasses of the retrieved-content defences: forged citation markers that render as
  verified, or invisible characters surviving into output.
- Redirect or URL handling that sends credentials somewhere unintended.
- Writes outside the session state directory, or any modification of the Codex credential
  file.
- Command injection or arbitrary file access through arguments or upstream responses.

Out of scope:

- The behaviour or availability of upstream search providers.
- Inaccurate or biased search results.
- Prompt injection that the skill correctly surfaces as untrusted content, where the
  documented defences behave as described in
  [references/SECURITY.md](skills/openai-web-search/references/SECURITY.md).

## Design commitments

These are properties the project intends to hold. A deviation is a bug worth reporting.

- No runtime dependencies beyond the Python or Node standard library.
- No telemetry, analytics, or update checks.
- No network request to any host other than the configured endpoint.
- The Codex credential file is opened read-only and never written.
- Credentials are never passed as command-line arguments.
