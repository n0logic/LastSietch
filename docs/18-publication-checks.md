# Check before publishing

A public push exposes content before hosted CI can inspect it. Run the local
gates first, then inspect the exact staged diff and outgoing commit metadata.
Never merge private repository history into this repository.

```sh
bash scripts/preflight.sh /path/to/candidate
git add path/to/reviewed-file
bash scripts/preflight.sh --staged
```

Directory mode includes ignored files such as local environment files. It
rejects symlinks, nested Git repositories, unreadable/special files, unexpected
binary content, prohibited data classes and oversized inventories. Build caches
and dependency directories are pruned; they are not publication candidates.
Use a fresh source-only staging directory, not a whole private checkout.

Staged mode scans every actual index blob. Editing a working copy after staging
does not change what that mode reviews. Missing or unresolved input is a failure,
never a clean result. Findings identify locations and classes without echoing
matched private values.

## Operator-specific inventory

Keep a private mode0600 JSON file outside the repository, with a nonempty patterns
array of regular expressions covering your own addresses, identities, private
paths and other forbidden values. Do not publish the inventory or put real values
into public test fixtures or deny rules. Use the required-inventory mode for
maintainer publication:

```sh
bash scripts/preflight.sh --staged \
  --private-patterns /path/to/private-inventory.json --require-private-patterns \
  --manifest /path/to/private-candidate-manifest.json
```

The manifest records clean candidate paths, sizes and hashes outside the public
tree. Verify it still matches the intended commit before pushing. The old
.preflight-local file is no longer an implicit input; convert it to the explicit
external JSON inventory rather than assuming it was loaded.

## Independent secret scan and tests

Run a trusted, checksum-verified Gitleaks binary on the staged changes and the
outgoing commit range. The one narrow taxonomy exception combines its path and
game-tag pattern; it does not exempt entire sender scripts or arbitrary content
in the taxonomy file. Tests inject a nonfunctional synthetic credential into
that same file and verify it is still detected.

```sh
GITLEAKS_BIN=/path/to/gitleaks python3 scripts/tests/test_public_preflight.py
gitleaks git --pre-commit --staged --redact=100 --ignore-gitleaks-allow --config .gitleaks.toml
gitleaks git --log-opts='origin/master..HEAD' --redact=100 --ignore-gitleaks-allow --config .gitleaks.toml
```

Review new author and committer names/emails too. A GitHub-provided noreply
address can avoid exposing a personal mailbox. Changing identity settings does
not alter old commits. Do not add private incident notes to commit messages.

Maintainers can install the versioned local hook with
git config core.hooksPath .githooks. Set PUBLIC_PUBLISH_INVENTORY and GITLEAKS_BIN
locally. The hook requires a clean working tree/index, checks the actual outgoing
HEAD, refuses unrelated history roots and non-noreply author/committer emails,
and scans commit messages as well as staged files. It uses the tested Gitleaks
version pinned in CI. It does not silently publish other branches or tags.
PUBLIC_BASE_REF defaults to the fetched origin/master reference; fetch it before
reviewing an update. Hook installation is local and must be repeated after cloning.

Current-tree checks do not certify historical commits, forks, caches, release
attachments or generated artifacts. Removing a file now does not erase its past
versions. Any history rewrite needs a separate reviewed plan; do not force-push
history as routine cleanup.

The automated gate is not a full application security or rights review. Review
dependencies, data provenance, output behavior and every new publication slice.
