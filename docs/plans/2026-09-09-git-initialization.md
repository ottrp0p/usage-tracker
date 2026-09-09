# Git initialization plan — 2026-09-09

Approved scope: version the existing project in `public` GitHub repository `ottrp0p/usage-tracker`, using an initial import branch and human PR review.

1. Preserve existing source and local data. Apply the reviewed ignore rules before staging.
2. Review candidate file contents and sizes; keep environments, secrets and local runtime/generated data outside Git.
3. Create an empty shared bootstrap baseline and import the reviewed source on `setup/initial-import`.
4. Establish hosted `main` at the empty baseline, push only the import branch's source, and open a draft PR. Never push directly to main/master or merge without human review.
5. Verify branch, tracked files, exclusions and remote visibility. Git setup does not require deployment or service changes.

Preserve five historical personal audit notes locally and exclude them from public history. Update README development links to public planning documentation and remove its obsolete no-Git statement. Synthetic tests and general technical documentation remain tracked.

## Import verification — 2026-09-09

Reviewed the complete candidate list, file sizes and credential patterns. Checked ignore matches for local data/cache examples; confirmed existing source and retained artifacts match their pre-import hashes. The import includes no file above 10 MiB. Original excluded data remains on disk. No application code or notebook output was changed, and application tests were not rerun for this Git-only setup.
