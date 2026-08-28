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

This document is scoped to specific, verifiable facts about the *current code*
that #404 does not state, and to a minimal first increment.

Every claim below has been checked against the tree at `upstream/main` and,
where datalad behaviour is load-bearing, against datalad 1.6.2 source. Claims
that could not be executed here are marked as such.

## What BABS actually requires of each side

**Input side (clone source):**

| Operation | Where | Needs |
|---|---|---|
| `datalad clone "${dssource}" ds --no-checkout` | `participant_job.sh.jinja2:53` | any URL `datalad clone` accepts |
| `datalad push --to input` | `bootstrap.py:440`, `update.py:40,104` | a pushable git remote |
| existence + hash check | `check_setup.py:198-201,236-241` | **a local directory** (see Finding 1) |

**Output side:**

| Operation | Where | Needs |
|---|---|---|
| `git remote add outputstore "$pushgitremote"` + `git push $BRANCH` | `participant_job.sh.jinja2:58,198` | a git URL, concurrently pushable |
| `datalad push --to output-storage` | `participant_job.sh.jinja2:192` | a reachable ORA endpoint (see Finding 4) |
| results-branch listing | `base.py:558` → `utils.py:402` | **a local directory** (see Finding 2) |
| `git branch --delete` after merge | `merge.py:376-383` | ref deletion in a local checkout |
| `datalad clone <output> merge_ds` | `merge.py:150` | any clonable URL |
| `git annex fsck -f output-storage` / `find --not --in output-storage` | `merge.py:297,319` | a named git-annex remote holding the content |

### Finding 1: the input store holds no annex content — but that does not make any bare repo usable

`bootstrap.py:191` creates the input sibling with `storage_sibling=False`:

```python
create_sibling_ria(
    name='input',
    url=self.input_ria_url,
    storage_sibling=False,  # False is `off` in CLI of datalad
    new_store_ok=True,
    **sibling_kwargs,
)
```

datalad turns that into `init_obj_tree=storage_sibling is not False`
(`create_sibling_ria.py:608-610`), so the input store's dataset directory gets
**no `annex/objects` tree at all**. The input "RIA store" is a bare git
repository under a RIA layout, holding no annexed content. The same asymmetry
exists in FAIRly big (`bootstrap_forrest_fmriprep.sh:82`,
`--storage-sibling off`), so it is inherited rather than a BABS quirk.

**#404 already states the principle**, at *Principles*:

> A plain Git endpoint can be a clone source only when it contains a viable
> DataLad dataset history, including the refs and subdataset information needed
> by a fresh job clone.

and distinguishes the roles' access needs under *Runtime roles* (clone source
"requires read/clone access"; result Git receiver "requires read, push, and
ref-enumeration access"). So the contribution here is not the principle but the
**code fact** — BABS's input store is *already* annex-free today — and the
scheduling conclusion that follows: #357's "input RIA only" and "neither" cases
need no annex machinery and could ship ahead of the output-side work.

**What is *not* true** is that any bare repo satisfies the input contract
today. Three things stand in the way:

1. `babs check-setup` reads the input store as a **local directory**:
   `check_setup.py:198-201` does `op.join(self.input_ria_path, data_foldername)`
   then `op.exists(...)`, and `check_setup.py:236-241` hashes it via
   `get_repo_hash()` → `git rev-parse HEAD` with `cwd=` that path
   (`utils.py:578-584`). A remotely-hosted clone source fails `check-setup`.
2. The clone source must carry the **`git-annex` branch**, because that is
   where the `output-storage` ORA remote is recorded with `autoenable=true`
   (see Finding 4). A job's `datalad push --to output-storage` works only
   because cloning the *input* store auto-enables it.
3. Jobs still symlink container images off the shared `analysis_path`
   (`participant_job.sh.jinja2`, container-linking block), so "no annex needed
   from the clone source" is contingent on the shared control plane #404
   mandates.

### Finding 2: results-branch listing is not endpoint-based on the live path

Three near-identically named helpers in `utils.py`:

| Function | Mechanism | Works remotely? | On the live path? |
|---|---|---|---|
| `get_results_branches(ria_directory)` (`utils.py:402`) | `git branch --list`, `cwd=` | **no** | **yes** |
| `get_results_branches_from_clone(clone_path)` (`utils.py:431`) | `git branch -r`, `cwd=` | no | no |
| `get_results_branches_from_ria(ria_data_dir)` (`utils.py:466`) | `git ls-remote --heads` | yes | **no — tests only** |

`base.py:556-558` — reached by `babs status`, `babs merge` (`merge.py:154`) and
`babs update-input-data` (`update.py:53`) — calls the **first**:

```python
def _get_results_branches(self) -> list[str]:
    """Get the results branch names from the output RIA in a list."""
    return get_results_branches(self.output_ria_data_dir)
```

`git branch --list` with a `cwd` cannot take a URL, so this is the hardest
local-only coupling in the status/merge path — harder than the RIA layout.

The best evidence that the live helper is the wrong one is in the test suite:
both e2e paths already route *around* it because it hangs.
`tests/test_babs_workflow.py:227-236` monkeypatches `_get_results_branches` to
use the clone variant ("Avoid running `git branch --list` in the RIA store (can
hang in CI)"), and `tests/test_update_input_data.py:191-202` uses the
`ls-remote` helper for the same reason.

**Ordering matters when fixing this.** #404 correctly flags that
`utils.py:493-494` (`if out.returncode != 0: return []`) turns an auth failure
into "no results". That swallow is in the *unused* helper; the live one uses
`check=True` and raises. So switching the live path onto the `ls-remote`
helper **before** fixing its error handling would introduce the
silent-empty-list hazard. Fix the swallow first, or land both together.

Two caveats for the switch: the branch-name format is identical (`job-…`
either way — traced through `status.py:261-287`, `merge.py:190`,
`update.py:53-55`), but `get_results_branches_from_ria` carries `timeout=30`,
adding a new `TimeoutExpired` failure mode to `babs status`; and
`get_results_branches_from_clone` is still imported by
`tests/test_babs_workflow.py:25`, so "retire the unused helpers" is not a
blanket delete.

### Finding 3: `wtf_key_info()` truncates non-`file` push URLs — latent, not yet reachable

`base.py:355-361` keeps only the path component of the `output` sibling's push
URL:

```
self.output_ria_data_dir = urlparse(
    proc_output_ria_data_dir.stdout.decode('utf-8')
).path.strip()
```

What datalad records there (verified in datalad 1.6.2,
`create_sibling_ria.py:765`):

```
url=str(repo_path) if url.startswith("ria+file") else git_url
```

with `pushurl=git_push_url`, which is `None` unless `--push-url` was passed —
so `git remote get-url --push output` returns the fetch URL. For `ria+file`
that is a bare path (the truncation is a genuine no-op). For `ria+ssh` it is a
full URL, and:

```
>>> urlparse('ssh://user@host/srv/babs-ria/238/da2f2-uuid').path
'/srv/babs-ria/238/da2f2-uuid'
```

`user@host` is discarded. That value reaches every job as `$2`
(`container.py:280`) and is used verbatim as `pushgitremote` →
`git remote add outputstore` → `git push outputstore "${BRANCH}"`.

**Severity, stated accurately:** this is *not* reachable today. `base.py:156-157`
hardcodes `'ria+file://' + path`, and `_resolve_subpath` (`base.py:47-57`)
rejects anything that is not a relative path inside `project_root`; there is no
CLI surface at all. So it is a **latent defect that blocks remote support** —
it must be fixed before any remote URL can be accepted — not a bug users can
hit now. An earlier draft of this document called it "an active defect today";
that was wrong.

### Finding 4: the annex channel is the thing that actually breaks first

This is the gap in the "just read the git URL" story, and neither this document
nor #404 named it concretely.

`create_sibling_ria.py:617-624` records the ORA special remote **in the
`git-annex` branch**:

```python
special_remote_options = [
    'type=external',
    'externaltype=ora',
    'encryption=none',
    'autoenable=true',
    'url={}'.format(url),
]
if push_url:
    special_remote_options.append('push-url={}'.format(push_url))
```

BABS passes neither `push_url` nor an actor-specific `url`, so the recorded
value is `ria+file:///<project_root>/output_ria` — **the controller's local
path** — baked into the git-annex branch with `autoenable=true`, and
auto-enabled in every job clone. The git sibling itself is marked
`remote.<name>.annex-ignore=true` (`create_sibling_ria.py:758-761`), which is
why the two channels exist at all.

Consequences:

- Moving the output store off the shared filesystem breaks
  `datalad push --to output-storage` regardless of what the git URL says. The
  `urlparse` truncation (Finding 3) is the *second* thing to break.
- `git remote get-url --push <sibling>` addresses only the **git** channel. It
  is still the right mechanism for that channel — git cannot push to
  `ria+file://…#id`, so whatever git recorded for a working sibling is already
  a real git URL — but it is not a complete answer to "where does this project
  publish?".
- `create_sibling_ria --push-url` exists precisely for the split-actor case,
  and is the concrete instance of #404's abstract requirement that "provider
  setup … does not assume its own local sibling URL is usable by a job".

Parameterising the *name* `output-storage` (step 3 below) does not address
this; the recorded **URL** needs actor-aware values.

### Inventory of local-only accesses

| # | Site | What it does | Local-only because | RIA? |
|---|------|--------------|--------------------|------|
| A | `base.py:156-157` | builds store URLs | literal `'ria+file://'` prefix | yes |
| B | `base.py:346-368` | `wtf_key_info()` derives `output_ria_data_dir` | `urlparse(...).path`, then `op.exists`/`op.realpath` on `alias/data` | partly |
| C | `container.py:278-280` | `dssource`, `pushgitremote` | hands compute nodes a bare local path | `#id` only |
| D | `bootstrap.py:450-457` | creates `output_ria/alias/data` | `os.makedirs` + `os.symlink` | yes |
| E | `bootstrap.py:156-157` | `.gitignore`s the store basenames | only meaningful inside `project_root` | no |
| F | `bootstrap.py:695-698`, `:702` | `babs init` failure cleanup | `op.exists(ria_path)`, then `rm -rf project_root` | no |
| G | `check_setup.py:185-247` | validates both stores | `os.readlink`, `op.exists`, `cwd=`-based hashing | partly |
| H | `merge.py:376-383` | deletes merged branches | `subprocess.run(..., cwd=self.output_ria_data_dir)` | no |
| I | `base.py:453-461` | `git safe.directory` registration | `Path(ria_root).glob('*/*')` | layout |
| J | `base.py:388-390` | `source_to_local_path` | only understands `ria+file://` / `file://` | no |
| K | `merge.py:297,319`, `participant_job.sh.jinja2:192` | annex content ops | ORA `url=` is the controller's local path (Finding 4) | yes |
| L | `utils.py:402`, via `base.py:558` | results-branch listing | `git branch --list` with `cwd=` | no |
| M | `merge.py:148-150` | `datalad clone` of the output store | *(none)* | `#id` only |

## Git-protocol equivalents

For the **git** channel only (Finding 4 covers the rest):

| Housekeeping today | Remote-capable equivalent |
|---|---|
| `cd <ria_data_dir> && git branch --delete …` (H) | `git push <url> --delete …` with an expected-OID lease |
| `git branch --list` with `cwd=` (L) | `git ls-remote --heads <url>`, without swallowing errors |
| `op.exists(<ria_data_dir>)` (G) | `git ls-remote --exit-code <url> HEAD` |
| `get_repo_hash(<ria_data_dir>)` (G) | `git ls-remote <url> HEAD` |
| `os.symlink(… alias/data)` (D) | `create_sibling_ria(..., alias='data')`, RIA-only |

None of these needs ssh shell access.

On the `alias/data` fallback at `base.py:365-368`: it was added in BABS
`5740ba2` (#337) with no stated reason, and datalad 0.14.0 → 1.6.2 all record
the *dataset* directory rather than the store root, so the case it guards
against is not one this document can demonstrate. Harmless to keep; an earlier
draft asserted a datalad-version rationale that is unsupported.

## Where this document would push back on #404

1. **Sequencing.** Remote RIA — the literal ask of #401 — first appears in
   Stage 3, behind capability probes, the spec/binding layer, and the full
   canonical-transaction rewrite. Findings 2 and 3 are small, isolated, and
   independently correct.
2. **Result refs under `refs/heads/`.** `refs/heads/babs/<project-id>/jobs/…`
   is matched by the default fetch refspec `+refs/heads/*:refs/remotes/origin/*`
   (verified empirically). Harmless while the result receiver is separate from
   the clone source, but #404 permits `reuse_dataset_target: clone_source` and
   the `kind: analysis` fallback, and there each job clone fetches other jobs'
   *unmerged* result refs and their objects. Publishing to `refs/babs/…` keeps
   them off the default refspec while staying enumerable via
   `git ls-remote <url> 'refs/babs/*'` (also verified). Costs worth weighing:
   some hosts reject pushes outside `refs/heads`/`refs/tags` (relevant to
   Stage 5's external providers); #404's merge step relies on
   `ls-remote --heads` and would need a pattern match; and such refs are
   invisible to `git branch -r`, `datalad siblings`, and most recovery tooling.
3. **The annex channel** (Finding 4) — the ORA `url=` is a stronger instance of
   #404's own actor-access-profile requirement than the git URL is.
4. **The unused-helper trap** (Finding 2) — fix the swallow before switching
   the live path onto it.

## A minimal first increment

**Step 1 — stop truncating the push URL** (Finding 3). Read
`git remote get-url --push output` whole. Behaviour-preserving today (the
`ria+file` value is already a bare path); it unblocks rather than fixes.

**Step 2 — make results-branch listing endpoint-based** (Finding 2). Fix the
error swallow first, then switch `base.py:558`. Format is compatible; mind the
new `TimeoutExpired` path and the `test_babs_workflow.py:25` import.

**Step 3 — make the content sibling a parameter, not a literal.** Replace the
hard-coded `output-storage` (`merge.py:297,319`,
`participant_job.sh.jinja2:192`) with a configured name. Necessary but **not
sufficient** — see Finding 4; the recorded ORA URL still needs addressing.

**Step 4 — lease-safe branch deletion.** Replace `git branch --delete` in
`cwd=output_ria_data_dir` (`merge.py:376-383`) with `git push <url> --delete`
against exact recorded OIDs, per #404 constraint 7. Note the semantic change:
`git branch --delete` refuses an unmerged branch, while a `--force-with-lease`
delete only checks the OID. `merge.py:285-287` pushes the merge before
deleting, so that check is a backstop rather than the primary guard — but it is
a real check being traded away.

Steps 2 and 4 are correctness fixes for existing local-RIA projects,
independent of remote support.

## Testing

- **Unit** — endpoint helpers are pure git plumbing over a URL, testable
  against `git init --bare` fixtures in `tmp_path` with no datalad: existence,
  head hash, branch enumeration, lease-safe deletion, and that an unreachable
  endpoint **raises** rather than returning `[]`.
- **Regression** — pin Finding 3: an `ssh://user@host/path` push URL must not
  lose `user@host`.
- **Existing e2e unchanged** — `tests/e2e-slurm/` must pass identically through
  the minimal increment. `tests/test_check_setup.py:52,83` construct
  `ria+file://…#<id>` URLs (as helper input, not assertions) and will need
  updating once addressing changes.
- **New e2e** — the slurm test container can host a `ria+ssh://localhost` store,
  exercising both the git and annex channels with no external infrastructure.

## Still open

1. **Ref namespace placement** — `refs/heads/…` vs `refs/babs/…`, with the
   costs above. Affects #404's frozen namespace.
2. **`babs init` failure cleanup with a remote store** (`bootstrap.py:695-702`).
   `rm -rf project_root` cannot reach a remote; suggest refusing to auto-delete
   and printing the exact command, consistent with #404's "cleanup never
   deletes a shared store root".
3. **`merge_ds` location** — potentially large; a scratch directory may beat
   `project_root` once the receiver is remote.
4. **The shared-`analysis_path` coupling** — container image symlinks,
   `DSLOCKFILE` (`container.py:267`), logs (`container.py:299-302`). #404
   covers this as its single-control-plane contract; recorded here only for
   completeness.
