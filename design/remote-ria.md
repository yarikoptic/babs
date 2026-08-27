# Remote input/output transport — code-level findings and a minimal first path

Notes toward [PennLINC/babs#401](https://github.com/PennLINC/babs/issues/401),
*"Make RIAs properly 'Remote'"*.

## Relationship to PR #404

[PennLINC/babs#404](https://github.com/PennLINC/babs/pull/404) (`design/ria.md`,
by @tien-tong) is the broader architectural design for this area, and it also
covers [#357](https://github.com/PennLINC/babs/issues/357) *"RIAs should be
optional"*, which this document originally missed entirely. **Where the two
disagree on architecture, #404 should win.** Its `TransportSpec` /
`TransportBinding` split, provider model, publication coordinator, merge
journal, and staged capability probes are a more complete treatment than what
was here before.

This document is now scoped to what it can contribute that #404 does not:

1. Specific, verifiable defects in the current code that #404 does not name —
   including one that is an active bug the moment anyone points BABS at a
   non-`file` URL.
2. A correction to the widely-assumed state of results-branch listing.
3. The observation that the input side's contract is strictly weaker than the
   output side's, which simplifies #357's four-way matrix.
4. A minimal first increment that delivers part of #401 without waiting on the
   full transaction rewrite.

Section [What #404 supplies that this document had wrong or
missing](#what-404-supplies-that-this-document-had-wrong-or-missing) records
what was adopted from it.

## What BABS actually requires of each side

Working backwards from every operation BABS performs:

**Input side** — used only by `participant_job.sh` and the canonical push:

| Operation | Where | Needs |
|---|---|---|
| `datalad clone "${dssource}" ds --no-checkout` | `participant_job.sh.jinja2:53` | any URL `datalad clone` accepts |
| `datalad push --to input` | `bootstrap.py:440`, `update.py:40,104` | a pushable git remote |

**Output side:**

| Operation | Where | Needs |
|---|---|---|
| `git remote add outputstore "$pushgitremote"` + `git push $BRANCH` | `participant_job.sh.jinja2:58,198` | a git URL, concurrently pushable |
| `datalad push --to output-storage` | `participant_job.sh.jinja2:192` | somewhere to put annex content |
| results-branch listing | `base.py:558` → `utils.py:402` | **see below — currently a local directory** |
| `git branch --delete` after merge | `merge.py:376-383` | ref deletion |
| `datalad clone <output> merge_ds` | `merge.py:150` | any clonable URL |
| `git annex fsck -f output-storage` / `find --not --in output-storage` | `merge.py:297,319` | a named git-annex remote holding the content |

### Finding 1: the input store holds no annex content

`bootstrap.py:191` creates the input sibling with `storage_sibling=False`
(`off` in the CLI):

```python
self.analysis_datalad_handle.create_sibling_ria(
    name='input',
    url=self.input_ria_url,
    storage_sibling=False,  # False is `off` in CLI of datalad
    new_store_ok=True,
    **sibling_kwargs,
)
```

So the input "RIA store" holds **no annexed content at all**. It is a bare git
repository that happens to sit under a RIA directory layout. Compute nodes
clone it for the refs and then pull data content from the input *subdatasets'*
own origins (`datalad get -n` per subject, then `datalad run`).

This matters for #357's four-way matrix. #404 states that "a code-only Git
repository is not a valid result destination" and that a result topology "must
be annex-capable" — both correct — but the input side carries no such
requirement, and #404 does not say so. **The clone source needs only a bare git
repo with viable DataLad history; it needs no annex capability, no ORA remote,
and no storage sibling.** "Input RIA only" and "neither" are therefore much
cheaper to implement than the output-side cases, and any bare repo (ssh,
GitHub, a path on a shared filesystem) already satisfies the input contract
today.

### Finding 2: results-branch listing is *not* endpoint-based on the live path

There are three near-identically named helpers in `utils.py`:

| Function | Mechanism | Works remotely? | On the live path? |
|---|---|---|---|
| `get_results_branches(ria_directory)` (`utils.py:402`) | `git branch --list`, `cwd=ria_directory` | **no** | **yes** |
| `get_results_branches_from_clone(clone_path)` (`utils.py:431`) | `git branch -r`, `cwd=clone_path` | no | no |
| `get_results_branches_from_ria(ria_data_dir)` (`utils.py:466`) | `git ls-remote --heads` | yes | **no — tests only** |

`base.py:556-558` — reached by `babs status`, `babs merge` (`merge.py:154`) and
`babs update-input-data` (`update.py:53`) — calls the **first** one:

```python
def _get_results_branches(self) -> list[str]:
    """Get the results branch names from the output RIA in a list."""
    return get_results_branches(self.output_ria_data_dir)
```

`git branch --list` with a `cwd` cannot take a URL at all, so this is the
hardest local-only coupling in the status/merge path — harder than anything in
the RIA layout itself.

Two consequences:

- #404 says "there is already a partial move toward endpoint-based branch
  lookup: `get_results_branches_from_ria()` uses `git ls-remote`". That
  function exists, but it is **currently unused outside `tests/`**
  (`tests/test_utils.py:247`, `tests/test_update_input_data.py:202,233`), so
  the live path has not moved at all.
- The error-swallowing #404 correctly flags (`utils.py:493-494`,
  `if out.returncode != 0: return []`, an auth failure reported as "no
  results") is real and must be fixed — but it sits in the *unused* helper. The
  live helper uses `check=True` and so raises instead. Fixing only the flagged
  one and then switching the live path to it would **introduce** the
  silent-empty-list hazard rather than remove it.

An earlier draft of this document asserted that `base.py:558` reached the
`ls-remote` helper and was therefore "already remote-capable". That was wrong;
the table above is the corrected reading.

### Finding 3: `wtf_key_info()` silently truncates non-`file` URLs

`base.py:355-361` reads the push URL datalad recorded for the `output` sibling
and discards everything but the path component:

```
self.output_ria_data_dir = urlparse(
    proc_output_ria_data_dir.stdout.decode('utf-8')
).path.strip()
```

For a local store that is a no-op. For an ssh remote it drops the host:

```
urlparse('ssh://user@host:/data/output_ria/238/da2f2-uuid').path
    == '/data/output_ria/238/da2f2-uuid'
```

The host is gone and BABS proceeds as though that path were local. The fallback
below it (`base.py:365-368`) — resolve `output_ria/alias/data` when the URL has
no `.git` — then fails to find the symlink and leaves the truncated path in
place.

`output_ria_data_dir` reaches every participant job as `$2`
(`container.py:280`) and is used verbatim:

```sh
pushgitremote="$2"	# i.e., `output_ria`
git remote add outputstore "${pushgitremote}"
flock "${DSLOCKFILE}" git push outputstore "${BRANCH}"
```

So this value *is* the URL each compute node pushes results to. Upstream
FAIRly-big's bootstrap uses `pushgitremote=$(git remote get-url --push output)`
unmodified; the `urlparse().path` is the only thing making it local.

**#404 does not mention this.** It describes `wtf_key_info()` parsing the
`output` sibling into `output_ria_data_dir` and proposes splitting its
responsibilities, which subsumes the fix architecturally — but the truncation
is an active defect today, not merely a design smell, and it is the single
smallest change that moves #401 forward.

### Inventory of local-only accesses

The **RIA?** column marks whether a site is coupled to RIA specifically or
merely to *locality*. Most are the latter.

| # | Site | What it does | Local-only because | RIA? |
|---|------|--------------|--------------------|------|
| A | `base.py:156-157` | builds store URLs | literal `'ria+file://'` prefix | yes |
| B | `base.py:346-368` | `wtf_key_info()` derives `output_ria_data_dir` | `urlparse(...).path`, then `op.exists`/`op.realpath` on `alias/data` | partly |
| C | `container.py:278-280` | `dssource`, `pushgitremote` | hands compute nodes a bare local path | `#id` only |
| D | `bootstrap.py:450-457` | creates `output_ria/alias/data` | `os.makedirs` + `os.symlink` | yes |
| E | `bootstrap.py:156-157` | `.gitignore`s the store basenames | only meaningful inside `project_root` | no |
| F | `bootstrap.py:695-698`, `:702` | `babs init` failure cleanup | `op.exists(ria_path)`, then `rm -rf project_root` | no |
| G | `check_setup.py:185-247` | validates the stores | `os.readlink`, `op.exists`, path-equality vs sibling URL | partly |
| H | `merge.py:376-383` | deletes merged branches | `subprocess.run(..., cwd=self.output_ria_data_dir)` | no |
| I | `base.py:453-461` | `git safe.directory` registration | `Path(ria_root).glob('*/*')` | layout |
| J | `base.py:388-390` | `source_to_local_path` | only understands `ria+file://` / `file://` | no |
| K | `merge.py:297,319`, `participant_job.sh.jinja2:192` | annex content ops | *(not local)* — hard-codes `output-storage` | yes |
| L | `utils.py:402`, via `base.py:558` | results-branch listing | `git branch --list` with `cwd=` | no |
| M | `merge.py:148-150` | `datalad clone` of the output store | *(none)* | `#id` only |

## The universal addressing mechanism

#404 states the constraint negatively — "no BABS command may infer an on-disk
repository path and operate inside a RIA layout", which is right. The positive
form is worth stating too, because it is a one-liner:

```
git --git-dir <analysis>/.git remote get-url --push <sibling>
```

Git cannot push to `ria+file://…#id` — that is not a git transport — so
whatever git has recorded for a working sibling is already a real, usable git
URL, whichever provider created it. Reading it **whole** works for RIA and
non-RIA, local and remote, with no layout knowledge, no `alias/data` symlink,
and no dataset id. This is both the fix for Finding 3 and the mechanism that
makes `output_ria_data_dir` unnecessary.

(Keep the `alias/data` resolution as a RIA-only fallback: `base.py:365-368`
shows some datalad versions record the store root rather than the dataset
directory.)

Once a URL replaces a path, the remaining housekeeping is plain git:

| Housekeeping today | Remote-capable equivalent |
|---|---|
| `cd <ria_data_dir> && git branch --delete …` (H) | `git push <url> --delete …`, with an expected-OID lease (see below) |
| `git branch --list` with `cwd=` (L) | `git ls-remote --heads <url>`, **without** swallowing errors |
| `op.exists(<ria_data_dir>)` (G) | `git ls-remote --exit-code <url> HEAD` |
| `get_repo_hash(<ria_data_dir>)` (G) | `git ls-remote <url> HEAD` |
| `os.symlink(… alias/data)` (D) | `create_sibling_ria(..., alias='data')`, RIA-only |

None of this needs ssh shell access.

## What #404 supplies that this document had wrong or missing

Adopted by reference rather than restated:

| Topic | Why it matters | #404 |
|---|---|---|
| **#357 optionality** and the four-way input/output matrix | Missed here entirely; changes the shape of the config model | *Problem summary*, Stage 3 |
| **`kind: analysis` fallback** — the shared `analysis` dataset as clone source and/or receiver | The "neither" case needs a real answer | *Configuration model* |
| **Pinning jobs to a canonical commit** | A queued job must not silently pick up newer code/inputs if the source advanced after submission — a correctness bug class not considered here | Constraint 9 |
| **Ref namespacing, immutability, lease-safe deletion** | `job-*` is a global namespace; unconditional `--delete` can remove another project's branch or a *replaced* ref | Constraint 7, *Project namespace* |
| **Merge journal, CAS canonical update, recoverability** | Multi-endpoint publication is not one transaction | *Merge transaction* |
| **`analysis` as canonical history** | Today the output RIA becomes the default-branch authority after merge and `analysis` catches up later | *Principles* |
| **Coordinator as an explicit role**; v1 restricted to `shared-flock` on a verified shared filesystem | Better than deferring it: `DSLOCKFILE` is not distributed, and an ssh URL does not supply a lock | Constraint 3 |
| **Store-root vs dataset-target vs logical-role identity** | Answers "can input and output be the same store?" properly — explicit assertion plus validation, never URL equality | Constraint 6 |
| **Secrets and argv** — no credentials in URLs/YAML/`set -x`; structured argv instead of `cmd.split()` | Not considered here | Constraint 8, Stage 1 |

**#404 also resolves an open question this document had raised.** The concern
was whether concurrent annex-content publication is safe at BABS scale. #404's
answer: the current RIA/ORA path is safe *because* jobs push only their result
refs and `babs merge` runs `git annex fsck --fast -f output-storage` **once** in
its merge clone to regenerate location metadata — so jobs never contend on a
shared `git-annex` branch. A provider without merge-time `fsck` needs job-time
metadata publication under the coordinator instead. That is the right framing
and supersedes the speculation previously recorded here.

## Where this document would push back on #404

Offered as review, not as a competing plan.

1. **Sequencing.** Remote RIA — the literal ask of #401 — first appears in
   Stage 3, behind a capability-probe stage, a spec/binding layer, and a full
   canonical-transaction rewrite. Findings 2 and 3 above are small, isolated,
   and independently correct; landing them first delivers real progress and
   reduces the risk that #401 waits on all of Stage 2.
2. **Result refs under `refs/heads/`.** `refs/heads/babs/<project-id>/jobs/…`
   is fetched by the default refspec (`+refs/heads/*:refs/remotes/origin/*`).
   That is fine while the result receiver is separate from the clone source,
   but #404 explicitly permits `reuse_dataset_target: clone_source` and the
   `kind: analysis` fallback — and in those configurations **every job's
   `datalad clone` fetches every other job's result refs**, which is O(jobs²)
   ref traffic on a 10k-subject study. Publishing to a namespace outside
   `refs/heads/` (e.g. `refs/babs/<project-id>/jobs/…`) keeps refs off the
   default fetch while remaining fully enumerable via
   `git ls-remote <url> 'refs/babs/*'`, at the cost of a slightly more explicit
   refspec on push. Worth deciding before the namespace is frozen.
3. **The unused-helper trap** (Finding 2). #404's fix for the
   error-swallowing is right, but the live path is a *different* function; the
   two changes must land together or the hazard is introduced rather than
   removed.
4. **Input-side asymmetry** (Finding 1). Stating that the clone source needs no
   annex capability would let Stage 3's "input RIA only"/"neither" cases ship
   ahead of the output-side work.

## A minimal first increment

Independently correct, independently mergeable, and compatible with #404's
architecture — each is work Stage 1/2 would otherwise have to do anyway.

**Step 1 — stop truncating the push URL** (Finding 3). Read
`git remote get-url --push output` whole; keep the `alias/data` resolution as a
`ria+file` fallback. Add a regression test asserting an
`ssh://user@host:/path` push URL does not lose `user@host`.

**Step 2 — make results-branch listing endpoint-based** (Finding 2). Switch
`base.py:558` to the `ls-remote` helper *and* fix its error handling in the
same change, so an auth or reachability failure raises instead of reporting an
empty result set. Retire whichever of the three helpers is then unused.

**Step 3 — make the content sibling a parameter, not a literal.** Replace the
hard-coded `output-storage` in `merge.py:297,319` and
`participant_job.sh.jinja2:192` with a configured name. Behaviour-preserving on
today's projects; a prerequisite for every non-RIA output provider.

**Step 4 — lease-safe branch deletion.** Replace `git branch --delete` in
`cwd=output_ria_data_dir` (`merge.py:376-383`) with
`git push <url> --delete` against exact recorded OIDs, per #404 constraint 7.

Steps 1, 2 and 4 are also correctness fixes for *existing* local-RIA projects,
independent of any remote support.

## Testing

- **Unit** — endpoint helpers are pure git plumbing over a URL, so they test
  against `git init --bare` fixtures in `tmp_path` with no datalad and no
  cluster: existence, head hash, branch enumeration, lease-safe deletion, and —
  importantly — that an unreachable or unauthenticated endpoint **raises**
  rather than returning `[]`.
- **Regression** — pin Finding 3: an `ssh://user@host:/path` push URL must not
  lose `user@host`.
- **Existing e2e unchanged** — `tests/e2e-slurm/` must pass identically through
  the minimal increment. Note `tests/test_check_setup.py:52,83` and
  `tests/test_merge.py:70,288` assert on `ria+file://…#<id>` URLs and will need
  updating once addressing changes.
- **New e2e** — the slurm test container can host both a `ria+ssh://localhost`
  store and a plain `git init --bare` repo over `ssh://localhost`, exercising
  the real `datalad clone` / `git push` path with no external infrastructure.

## Still open

1. **Ref namespace placement** — `refs/heads/…` vs `refs/babs/…` (push-back 2).
   Affects #404's frozen namespace, so worth settling early.
2. **`babs init` failure cleanup with a remote store** (`bootstrap.py:695-702`).
   `rm -rf project_root` cannot reach a remote. Suggest refusing to auto-delete
   remote state and printing the exact command instead; #404's rule that
   "cleanup never deletes a shared store root" points the same way.
3. **`merge_ds` location** — cloned from the output receiver and potentially
   large; a scratch directory may beat `project_root` once the receiver is
   remote. Orthogonal but touched by the same code.
4. **The shared-`analysis_path` coupling** — container image symlinks
   (`participant_job.sh.jinja2`), `DSLOCKFILE` (`container.py:267`), and logs
   (`container.py:299-302`). #404 covers this as its first-release
   single-control-plane contract and scope boundary; recorded here only so the
   inventory is complete.
