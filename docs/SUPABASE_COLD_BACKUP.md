# Supabase weekly cold backup and recovery

This workflow creates one encrypted logical database backup every Sunday at 02:00 UTC (10:00 China time), keeps releases published within the last 30 days, and never prunes old backups until a new backup has been exported, encrypted, decrypted, checksum-verified, restored into an isolated Supabase stack, and passed row-count checks.

## What is backed up

Each private GitHub Release in `sizhitu/Autopilot-data` contains one asset:

- `database-backup.tar.zst.age`: an age-encrypted zstd tar archive.
- Inside: `roles.sql`, `schema.sql`, `data.sql`, `migration_history_schema.sql`, `migration_history_data.sql`, `manifest.json`, and `SHA256SUMS`.

The Supabase CLI exports database roles, application schema and data. Supabase migration history is exported separately, following Supabase's documented optional migration-history procedure.

**Important scope limitation:** this is not a physical, byte-for-byte backup of the entire Supabase project. The CLI filters Supabase-managed schemas such as `auth` and `storage`; Auth users, Storage metadata/object bytes, Edge Functions, Auth provider configuration, SMTP settings, API keys, and project-level settings are not included. Supabase's project-clone flow can copy Auth user data, but this GitHub workflow does not currently do that. If Auth users or Storage objects are business-critical, they need a separate backup/restore process and a separate test before this can be called a complete project disaster-recovery solution.

The archive may contain sensitive application data. It is encrypted before upload and stored only in the private `Autopilot-data` repository, never in the public application repository.

## One-time GitHub configuration

In `sizhitu/Autopilot` → Settings → Secrets and variables → Actions:

### Required secrets

1. `SUPABASE_DB_URL`: PostgreSQL Session Pooler connection string from Supabase Dashboard → Connect. URL-encode special characters in the password. GitHub-hosted runners are IPv4-only, so use Session Pooler unless your project has the IPv4 add-on.
2. `SUPABASE_BACKUP_AGE_IDENTITY`: the complete private age identity file content. The workflow needs this to decrypt the exact uploaded artifact and prove that it can be unpacked and restored. Keep a separate offline copy in at least two secure locations. The private key is available to the workflow as a GitHub Actions secret; anyone who can change trusted workflow code or access this secret can decrypt backups.
3. `DATA_REPO_TOKEN`: existing token with Contents read/write access to the private `sizhitu/Autopilot-data` repository, including Release create/delete permissions.

### Required repository variable

Create an age key pair on a trusted machine:

```bash
age-keygen -o supabase-backup-identity.txt
```

Add the public `age1...` recipient as Actions repository variable `SUPABASE_BACKUP_AGE_RECIPIENT`. Keep the identity file offline as a recovery copy. Never commit it.

## Backup inventory and restore validation reports

Every successful backup release publishes readable Markdown, machine-readable JSON, and per-table CSV reports:

- `before_backup.md/json/csv`: captured from the live database immediately before export.
- `after_restore.md/json/csv`: captured from the isolated restored database.
- `restore_comparison.md/json`: pass/fail result with before/after database size, table count, index count, total rows, and all table-level mismatches.
- `artifact_sizes.md/json`: byte and MiB sizes for each SQL dump, manifest/checksum, compressed archive, and encrypted upload.

Each per-table inventory includes schema/table name, exact row count, index count, table bytes, index bytes, and total relation bytes. The database-wide size is PostgreSQL's full database size (so it can include managed schemas excluded from this logical export); the per-table inventory is limited to the schemas this workflow checks. The verifier strictly requires the same table list, per-table row counts, per-table index counts, total table count, and total index count. Any difference fails the workflow and prevents upload/pruning. Physical database/table/index byte sizes are recorded for capacity tracking but are not required to match byte-for-byte after restore because PostgreSQL may lay out or rebuild relations differently.

The reports cover the application/migration schemas exported by this CLI backup; they do not expand the backup scope to Supabase-managed Auth/Storage data.

## Automated verification performed before upload

1. Export non-empty roles, schema, data and migration-history SQL files.
2. Build a manifest and SHA-256 checksums.
3. Compress with zstd and encrypt with age.
4. Decrypt the exact encrypted file using the Actions identity secret, extract it into a clean directory and verify every checksum.
5. Start a disposable local Supabase stack.
6. Restore roles, schema and data in Supabase's documented order, then restore migration history in a separate transaction.
7. Parse the COPY blocks in the exported data SQL and compare the expected row count for every exported table with the restored local table's actual row count.
8. Only after all checks pass, upload the encrypted artifact to a Release in the private data repository, confirm that the asset exists, and prune backup releases older than 30 days.

The local restore is a structural/data-integrity test. It does not validate Auth users, Storage objects, Edge Functions, provider settings, or every project-level setting because those are outside this backup's scope.

## Manual restore procedure

**Never restore directly over production as the first test.** Create a new/empty Supabase project or a separate recovery project first. Enable any non-default extensions and Database Webhooks used by the original project before restoring.

1. Download `database-backup.tar.zst.age` from the private `Autopilot-data` Release.
2. Install `age`, `zstd`, `tar`, and `psql`.
3. Decrypt and extract:

```bash
age -d -i supabase-backup-identity.txt database-backup.tar.zst.age | tar --zstd -xf -
sha256sum --check SHA256SUMS
```

4. Set the new project's PostgreSQL connection string:

```bash
export NEW_DB_URL='postgresql://...'
```

5. Restore the main database dump using the documented order:

```bash
psql "$NEW_DB_URL" -X \
  --single-transaction \
  --variable ON_ERROR_STOP=1 \
  --file roles.sql \
  --file schema.sql \
  --command 'SET session_replication_role = replica' \
  --file data.sql
```

6. Restore migration history separately:

```bash
psql "$NEW_DB_URL" -X \
  --single-transaction \
  --variable ON_ERROR_STOP=1 \
  --file migration_history_schema.sql \
  --file migration_history_data.sql
```

7. Review Supabase restore notes for role grants/ownership. Custom login roles may need their passwords reset. Re-enable any Realtime publications used by the project.
8. Reconfigure project-level Auth providers, SMTP, API keys, Edge Functions, secrets and Storage separately. Copy Storage object bytes using a separate Storage export process; database dumps alone do not contain them.
9. Run application smoke tests against the recovery project before switching production traffic.

Supabase's official restore guide: https://supabase.com/docs/guides/platform/migrating-within-supabase/backup-restore

## Retention and failure behavior

- Weekly schedule: Sunday 02:00 UTC (10:00 China time); manual dispatch is available.
- Retention: delete only backup releases older than 30 days.
- Failed export, decryption, checksum, local restore, row-count comparison, or upload stops the workflow. Existing backups are not pruned.
- The backup asset is limited to 1.9 GB as a safety check.
- The workflow serializes runs to prevent overlapping backup/prune operations.
