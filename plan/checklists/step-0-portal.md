# Step 0 manual checklist (non-scriptable / portal-only)

Record completion with a date. Write no tenant specifics here.

## A. Local Git identity + SSH
- [ ] `git config --local user.email` = `<id>+<user>@users.noreply.github.com`; `git config --local user.useConfigOnly true`
- [ ] SSH key `~/.ssh/id_ed25519_github_personal` added to the **personal** GitHub account (Settings → SSH keys)
- [ ] `~/.ssh/config` has `Host github-personal` → `github.com`, `IdentitiesOnly yes`
- [ ] `ssh -T git@github-personal` greets the personal account
- [ ] Test commit pushed: `git log -1 --format='%an <%ae>'` shows the no-reply address
- [ ] GitHub → Settings → Emails: "Keep my email private" + "Block command line pushes that expose my email" ON

## B. Fabric Admin portal (only items `tenant_settings.py` reports as MANUAL)
- [ ] Copilot and Azure OpenAI features (+ cross-geo processing if region requires)
- [ ] Data agent item types (preview)
- [ ] Ontology item (preview)
- [ ] Users can sync workspace items with GitHub repositories
- [ ] Service principals can call Fabric public APIs → *specific security group* `ce-fabric-automation`
- [ ] Service principals can create workspaces, connections, and deployment pipelines → same group
- [ ] Workspace monitoring; endorsement/certification (certifier group = `ce-fabric-admins`)

## C. GitHub fine-grained PAT for Fabric Git integration
- [ ] github.com → Settings → Developer settings → Fine-grained tokens → *Only select repositories*: this repo → Repository permissions: **Contents: Read and write** (Metadata read is implicit)
- [ ] Expiry ≤ 90 days; put the value in `.env` as `GITHUB_FABRIC_PAT` only. Rotate by updating the Fabric connection (Settings → Manage connections).
