# Security and privacy

Insight Harness is a synthetic-data demonstration, not a hardened healthcare or
commercial production system. It has no application identity provider,
authorization roles, tenant database, PHI workflow, or secrets vault. Deploy it
with synthetic data unless those controls have been added and reviewed.

## Secure defaults

- No API credential is bundled or committed.
- The deterministic parser and all analytical surfaces work without a model.
- A deployment-owned Anthropic key is not available to anonymous sessions unless
  `INSIGHT_HARNESS_ALLOW_PUBLIC_LLM=true` is explicitly configured.
- Model IDs are allowlisted and model calls are bounded per Streamlit session.
- Governance is read-only and causal analyst sign-off is locked unless the
  server has a governance administrator token and the operator enters that
  token in the current session.
- Feedback excludes raw question text by default.
- Saved tiles, layouts, watches, and public digest history remain in the current
  Streamlit session instead of a container-global user profile.

Copy `.env.example` or `.streamlit/secrets.toml.example` for the available
settings. Never commit `.env`, `.streamlit/secrets.toml`, API keys, or admin
tokens; those paths are ignored as a second line of defense, not as a secrets
management system.

## Model data boundary

For question translation, the app sends the user's question and the registered
metric, dimension-value, and event vocabulary to Anthropic. It does not send
source rows. The returned structured intent is registry-validated; invalid or
unavailable output falls back to the deterministic parser before an engine runs.

For optional digest phrasing, the app sends a bounded computed-fact payload. The
rewrite is rejected unless it preserves the governed metric and scope, every
number/sign/unit, the computed direction, and non-causal wording. Models never
rank digest items or compute answers.

An API key entered in the sidebar is kept in Streamlit session state and is not
written to disk by the application. Use it only on a deployment you trust. A
deployment key should have provider-side spend limits and monitoring in addition
to this app's session quota.

Downloaded answer and digest artifacts may contain the original question,
structured model output, computed facts, and scope labels. Treat them according
to the sensitivity of the question even though the bundled dataset is synthetic.

## Governance boundary

`INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN` protects writes to shared materiality
and default-variant configuration and unlocks causal analyst sign-off. Token
comparison is constant-time. Accepted governance changes and their bounded audit
history are committed together via a locked atomic replace; an fsynced local
JSONL file provides an append-friendly export mirror. An authenticated sign-off
records provenance against the exact result hash and data version; it does not
promote a Hypothesis-tier result.

This shared secret is a demo control, not user authentication or role-based
access. For production, replace it with authenticated identities, authorization,
CSRF-aware administration, an external transactional store, and an audit trail
whose retention and access are governed outside the web process.

## State and telemetry

The public UI keeps user-shaped state in memory per Streamlit session. It is
isolated between sessions but is ephemeral and disappears when a session or app
process ends.

Governance configuration/logs, feedback, and Reliability run history are local
JSON/JSONL files. They are shared by viewers of the same running deployment and
may be ephemeral on managed hosting. Governance and feedback writers use local
locks to reduce concurrent-write corruption; this does not provide tenant
isolation, encryption, retention policy, backup, or disaster recovery.

Feedback records a keyed HMAC of the question plus artifact metadata and the
verdict. When `INSIGHT_HARNESS_TELEMETRY_HASH_KEY` is unset, the app generates a
random key per process, so identifiers cannot be correlated reliably across
restarts. Stable correlation is opt-in by configuring that variable with a long
random secret; doing so creates a persistent pseudonymous identifier and
requires an approved purpose, access controls, rotation, and retention policy.

Raw question retention is separately opt-in with:

```text
INSIGHT_HARNESS_LOG_RAW_QUESTIONS=true
```

Leave it false on public deployments unless there is an approved purpose,
notice, retention period, and access policy. Free-text feedback notes can also
contain sensitive content and should be handled accordingly.

## Public deployment checklist

- Keep the committed synthetic dataset; do not upload real HCP, patient, or
  confidential commercial data to an anonymous app.
- Store secrets in the hosting platform, not the repository.
- Leave public deployment-key use off unless anonymous model spend is intended.
- Set a conservative model-call limit and provider-side budget alerts.
- Configure a long random governance token only if shared writes are required.
- Restrict network and repository permissions to the minimum needed.
- Decide whether local telemetry should be disabled, exported, or backed by a
  controlled external service.
- Review artifact downloads, server logs, and error reporting for question text.
- Add authentication, authorization, rate limiting, durable storage, encryption,
  retention, monitoring, and incident response before production use.

## Reporting a vulnerability

Report suspected vulnerabilities privately to the repository owner or through
the repository's private security-reporting channel. Do not include live secrets,
real personal data, or confidential customer data in an issue or reproduction.
