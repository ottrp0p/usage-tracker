# Usage Tracker project instructions

## Deployment

- Always deploy this project at **http://localhost:8787**. The user corrected
  the mistaken 8447 instruction on September 9, 2026; it was for another project.
- Before deployment, stop only the recognized Usage Tracker LaunchAgent and
  redeploy to **8787**. Do not kill unrelated listeners on either port. If 8787
  is occupied by another process, report the conflict; do not change ports.
- Use `python3 -m usage_tracker service install` for macOS deployment/redeployment;
  it owns the stop, bind-check, and restart sequence. Preserve the existing data
  directory and source configuration. Verify `/api/health` afterward.
- Ephemeral ports are permitted for isolated automated tests, never deployment.

## Working style

- Keep user-facing answers short and focused on the most salient findings.
- Write implementation plans in `docs/plans/` before editing and append results.
- Comment important code blocks so the user can review the reasoning in place.
- Use development branches when Git is present; never push directly to main or
  master. Human PR review is the merge gate. Do not initialize Git implicitly.
