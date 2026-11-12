# Supabase weekly cold backup

This workflow creates one encrypted logical database backup every Sunday at 02:00 UTC (10:00 China time), keeps releases published within the last 30 days, and never prunes old backups until a new backup has been exported, verified, encrypted, and published successfully.

## Scope and format

Each private GitHub Release contains one asset:

- `database-backup.tar.zst.age`: an age-encrypted zstd tar archive.
- Inside the archive: `roles.sql`, `schema.sql`, `data.sql`, `migration_history_schema.sql`, `migration_history_data.sql`, `manifest.json`, and `SHA256SUMS`.

The Supabase CLI is used rather than raw `pg_dump`. It filters Supabase-managed/internal schemas and produces separate role, schema, and data exports. The data export excludes `storage.buckets_vectors` and `storage.vector_indexes` as recommended by Supabase. The `supabase_migrations` schema and its rows are exported separately so migration history can be preserved.

**This is a logical PostgreSQL database backup, not a complete Supabase project export.** It does not contain the bytes of files stored in Supabase Storage, Edge Function source/secrets, Auth provider configuration, SMTP settings, API keys, or project-level settings. Storage object bytes and Edge Functions need a separate backup plan if they become part of the recovery requirement.

The database data can contain personal information and Auth password hashes. The archive is encrypted before upload and must only be published to a private repository.

## One-time GitHub configuration

In `sizhitu/Autopilot` → Settings → Secrets and variables → Actions:

### 1. Add repository secret `SUPABASE_DB_URL`

Copy the **Session pooler** connection string from Supabase Dashboard → Connect → Session pooler. Use the database password and URL-encode special characters in it.

GitHub-hosted runners are IPv4-only; Supabase recommends the Session pooler connection string when a project does not have the IPv4 add-on. Do not use the service-role API key here; this must be a PostgreSQL connection string.

### 2. Create an age key pair and add the public recipient

On a trusted local machine, install age and run:

```bash
age-keygen -o supabase-backup-identity.txt
```

The command prints a public recipient similar to `age1...`. Add that public recipient as an Actions **repository variable** named:

```text
SUPABASE_BACKUP_AGE_RECIPIENT
```

Keep `supabase-backup-identity.txt` offline in a secure place (preferably two secure copies). It is the private decryption key. Never commit it or upload it to the repository. If it is lost, the encrypted backups cannot be decrypted.

## First run

After both settings are added:

1. Open Actions → Supabase weekly cold backup.
2. Select Run workflow.
3. Wait for all steps to pass, including the isolated local restore check.
4. Open Releases and confirm a new `supabase-db-YYYYMMDDTHHMMSSZ` release with `database-backup.tar.zst.age`.

The workflow runs automatically every Sunday at 02:00 UTC. It prunes only releases with the `supabase-db-` prefix that are older than 30 days. Other releases are untouched.

## Restore procedure

Restore into a **new/empty target database or a separate Supabase project first**. Do not test a restore against production.

Install `age`, `zstd`, `tar`, `psql`, and the Supabase CLI. Download the release asset from the private GitHub repository, then decrypt and extract:

```bash
age -d -i supabase-backup-identity.txt database-backup.tar.zst.age | tar --zstd -xf -
sha256sum --check SHA256SUMS
```

For a new Supabase project, follow the current Supabase Backup and Restore guide and apply in order:

1. `roles.sql`
2. `schema.sql`
3. `data.sql`
4. `migration_history_schema.sql`
5. `migration_history_data.sql`

Use the target project's Session pooler connection string with `psql`. Review Supabase's current restore guidance for role/privilege and extension differences before restoring. Project-level Auth settings, Storage object bytes, Edge Functions, and secrets must be restored separately.

## Operational safeguards

- Schedule is weekly; manual dispatch is available for the first run and on-demand backups.
- A single run is serialized; overlapping runs are not allowed.
- The workflow checks all export files are non-empty, verifies SHA-256, tests archive integrity, and runs a restore into a disposable local Supabase stack.
- It uploads one encrypted asset to a private GitHub Release and confirms the asset exists before pruning.
- Any export, restore-test, encryption, upload, or verification failure stops the workflow and leaves existing releases untouched.
- A 1.9 GB safety limit is enforced for the single release asset.
