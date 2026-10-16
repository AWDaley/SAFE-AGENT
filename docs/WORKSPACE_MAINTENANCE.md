# Local workspace maintenance and billing-keyring distribution

Status: explicit offline maintenance only. These commands do not authorize live
providers, hosted inference, real billing, or production execution.

## Principles

Normal SAFE-AGENT startup never migrates a workspace. If the installed runtime
expects a newer schema, `serve`, `status`, session issuance and normal application
startup fail closed.

A schema transition is a separate operator action that:

1. acquires the API, worker and operation locks;
2. verifies the legacy workspace manifest and key hashes;
3. verifies the exact legacy database head and SQLite integrity;
4. creates an owner-only, HMAC-authenticated backup;
5. migrates a staged database copy;
6. verifies the staged schema and integrity;
7. writes an authenticated upgrade journal;
8. atomically replaces the live database and manifest metadata;
9. reopens and verifies the current workspace before reporting success.

If an upgrade is interrupted after the journal is written, later upgrades fail with
`UPGRADE_ROLLBACK_REQUIRED`. There is no automatic guess about whether a partial
upgrade is safe.

## Upgrade 0013 to 0014

All SAFE-AGENT API and worker processes must be stopped.

```sh
safe-agent-local --workspace /private/safe-office \
  upgrade \
  --migrations "$PWD/migrations" \
  --backup /private/backups/safe-office-pre-0014
```

The backup path must not already exist and cannot be the workspace, its parent, or
a child of the workspace. The backup directory is created mode 0700; its files are
0600.

The backup contains the consistent pre-upgrade SQLite database plus the exact
`workspace.json`, `workspace.mac` and `READY` bytes. `backup.json` records their
digests, source/target schema heads and the workspace key hashes. `backup.mac`
authenticates that record with the existing broker key.

No workspace key is rotated by this migration.

## Rollback

Rollback is explicit and restores only a verified backup:

```sh
safe-agent-local --workspace /private/safe-office \
  rollback \
  --backup /private/backups/safe-office-pre-0014
```

The backup MAC, database digest, metadata digests, key hashes, database integrity
and source migration head are verified before replacement.

After rollback the workspace is intentionally back at schema 0013. The current
0014 runtime therefore refuses normal startup until the operator either runs the
reviewed upgrade again or deliberately uses the matching older reviewed runtime.
Rollback is not an implicit compatibility shim.

## Billing keyring distribution root

Verifier public-key keyrings are distributed under a separate offline Ed25519
distribution root. The distribution private key does not belong in the SAFE-AGENT
workspace.

Pin the public root once:

```sh
safe-agent-local --workspace /private/safe-office \
  pin-billing-root \
  --key-id billing_distribution_2026_v1 \
  --public-key-file /private/admin/billing-distribution-public.hex
```

The public-key file is 32 raw bytes encoded as 64 lowercase hexadecimal characters
and must be an owner-only regular file.

The workspace stores the pinned root, its fingerprint and an HMAC over the root
record. A different root cannot replace it through this command.

## Signed keyring bundles

A bundle contains a bounded `BillingVerifierKeyringConfig`, a monotonically
increasing generation, the previous bundle digest, issuance/expiry timestamps and
an Ed25519 signature from the pinned distribution root.

Generation 1 has no predecessor. Every later generation must be exactly:

```text
generation = installed_generation + 1
previous_bundle_digest = installed_bundle_digest
```

Install a reviewed signed bundle:

```sh
safe-agent-local --workspace /private/safe-office \
  install-billing-keyring \
  --bundle /private/admin/billing-keyring-generation-2.json
```

The workspace persists the signed bundle and an HMAC-authenticated installed state.
Reinstalling an old signed bundle, skipping a generation, breaking the digest chain,
using another distribution key or installing an expired/not-yet-valid bundle fails
closed.

The application model, browser/operator API, inference sidecar and execution worker
have no endpoint that can pin a root or install a keyring.

## Rotation semantics

The installed keyring still applies the verifier-key epoch rules from PR #42:
an attestation's `observed_at` must fall inside its verifier key's signing epoch,
and reconciliation must occur before that key's bounded `accept_until`.

Distribution-bundle expiry is an additional control. A valid verifier key does not
make an expired distribution bundle acceptable.

## Recovery and threat boundary

The upgrade backup is intentionally separate from the live workspace. It does not
contain workspace secret keys; rollback requires that the live workspace keys still
match the backed-up manifest hashes.

Signed bundle generation chaining prevents ordinary installation of an older bundle.
It does **not** provide hardware-backed monotonic storage: a privileged filesystem
snapshot rollback by the owning UID/root could roll back both bundle and state.
Production deployment still needs a stronger external configuration authority or
monotonic anchor, separate service identities, and reviewed key-rotation ceremony.

The current billing source remains synthetic. No real provider invoice adapter or
billing credential is enabled by these maintenance commands.
