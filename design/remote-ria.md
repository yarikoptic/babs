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

## Implementation notes (verified while building it)

The minimal increment above is implemented on this branch, with a plain bare
git repository supported as an output remote (`babs init --output-remote`).
The code is split the way the two channels are: `babs/git_endpoint.py` is pure
git plumbing against a URL (no datalad, no filesystem assumptions), and
`babs/output_remote.py` holds the two providers — `RiaOutputRemote` (the
unchanged default) and `BareGitOutputRemote`. Everything below was **measured**
against git-annex 10.20240129 / datalad 1.6.2, not reasoned from source.

**Confirmed as designed.** `git remote get-url --push output` returns the
dataset directory `<store>/<id[:3]>/<id[3:]>` (a bare path for `ria+file`, so
the old truncation was a no-op locally); `remote.output.annex-ignore=true` on
the RIA git sibling; and the ORA remote is recorded in the `git-annex` branch
as `url=ria+file:///<project_root>/output_ria` with `autoenable=true` and no
`push-url` — Finding 4, reproduced end to end.

**A bare repo must be `git annex init`-ed, or content is silently lost.**
Pushing to a bare repository that has not been annex-initialized reports
`copy (notneeded)` and transfers **zero** annex objects while still pushing the
branches. The run looks successful and the refs are there, but a fresh clone
cannot `git annex get` the results. The same repository refuses loudly under
`git annex copy --to` (`cannot determine uuid`, rc=1) — the silent path is the
one the job script takes, which is why `BareGitOutputRemote.create_sibling()`
initializes the repository and refuses to continue if no `annex.uuid` results.

**The content sibling's name is context-dependent.** This was not in the design
and is the one thing that made the first job fail (`Unknown push target
'output'`). A RIA ORA remote is auto-enabled from the git-annex branch, so
`output-storage` resolves in every clone — controller, job and `merge_ds`
alike. A plain remote has no such record and is known only by whatever each
clone calls it:

| clone | RIA (ORA) | plain remote |
|---|---|---|
| `analysis` | `output-storage` | `output` |
| participant job | `output-storage` | `outputstore` (added by the job script) |
| `merge_ds` | `output-storage` | `origin` (it is cloned *from* the remote) |

Hence the name is asked of the provider per context rather than being a single
constant.

**Content-only transfer preserves the two-phase publication.** For a plain
remote `datalad push --to outputstore` would publish refs too, destroying the
property that the result ref is the last, locked, completion marker. The job
script uses `git annex copy --to outputstore .` instead — verified to move
content while pushing no refs, after which the locked `git push outputstore
$BRANCH` publishes the result. The RIA path still uses
`datalad push --to output-storage`, unchanged.

**`git push` needs a repository context, even with an explicit URL.**
`git ls-remote <url>` works from anywhere; `git push <url> …` does not — it
fails with *"fatal: not a git repository"*. Lease-safe deletion is therefore
run with an explicit `cwd`. This one bit twice: unit tests pass without it
because pytest runs inside a repository, and the e2e script passes because it
`cd`s to the repository root, so the failure only appears as
`cd ~ && babs merge <project>` — which merged, pushed, printed *"`babs merge`
was successful!"*, then died, leaving every `job-*` branch in place. It affects
the **RIA default**, not just the new provider.

**A pre-existing bare repo's `HEAD` may be unborn.** `git init --bare` points
`HEAD` at `init.defaultBranch`, and pushing a differently named branch does not
update it; `git ls-remote <url> HEAD` then prints nothing *and exits 0*.
Probing `HEAD` for reachability therefore reports a fully populated remote as
missing, and `git remote show` reports no default branch, which stops
`babs merge`. The reachability probe here is still `ls-remote <url> HEAD`, so
the provider repairs `HEAD` instead: after the first push it points it at a
published branch that is not `git-annex` (never what a reader of the results
wants). That repair is a local `git symbolic-ref`, so a *pre-existing* endpoint
reached only by URL would still be misreported as empty — a residual risk that
becomes real exactly when URL endpoints are allowed, and the reason to prefer
"does it advertise any ref" as the probe.

**Classifying a git URL is not `urlparse`'s job.** `git@host:out.git` has no
scheme and no `://`, so `urlparse` reports it as a bare path — which would make
provisioning create a local directory literally named `git@host:out.git` while
every job pushes over ssh to the real host, and that host never gets
`git annex init`-ed. `user@host:/srv/x` and `host:/srv/x` have the same shape.
The check applies git's own rule (no `://`, colon before the first slash). A
relative path has the mirror problem: git resolves it against `analysis/`, not
the directory `babs init` ran in, so the value is made absolute.

**The deletion lease must use the OIDs from enumeration.** Re-reading the
branch OIDs immediately before `git push --delete` pins whatever the branches
point at by then, which makes the lease a no-op by construction. `babs merge`
captures them where it lists the branches, and the push is `--atomic`, so a
single refused lease cannot leave the rest of a chunk deleted with the caller
unable to tell which.

**What `--output-remote` refused.** The first cut of this branch accepted only
a local path, refusing `ria+` URLs and every other non-local URL alike. That
was a narrowing of this design rather than a property of it; the sections below
record how it was reviewed and what replaced it.

## Review of the implementation — what it changes here

The implementation on this branch was reviewed by @yarikoptic
([review](https://github.com/yarikoptic/babs/pull/2#pullrequestreview-5114526953)).
Several points land on this design rather than on that code, so they are
recorded here.

**The endpoint taxonomy is four-way, not two-way.** This document framed the
choice as "RIA store, or plain bare git repository". That is too narrow in two
directions at once, and the review names both: an endpoint reachable only by
URL (a forgejo+aneksjo instance — the literal ask of #401), and a *non-bare*
local repository that receives pushes and updates its worktree. The dispatch
should be on the shape of the value, with the URL/path split deciding
create-vs-validate:

| `--output-remote` value | BABS does |
|---|---|
| URL (`://`, or git's scp-style syntax) | never creates. Validates: reachable, and advertises `refs/heads/git-annex` |
| local path ending in `.git` | creates/manages a plain bare repository |
| local path not ending in `.git` | creates/manages a regular repository with a worktree, `receive.denyNonFastforwards=true` + `receive.denyCurrentBranch=updateInstead` |
| local path that is (or should be) a RIA store | the RIA provider — still the default |

**The narrowing, and its removal.** The rule underneath that table — *create
locally, validate remotely* — is the one this design intends, and BABS can only
*guarantee* `git annex init` on an endpoint it can reach as a path. But that
argument bounds creation only: an endpoint that already **is** an annex can be
validated instead. `BareGitOutputRemote.__init__` raised on every non-local URL,
so the first cut added a second **local** provider and left #401's literal ask
unmet. That is now fixed — see "The dispatch as built" below.

**`receive.denyCurrentBranch=updateInstead` works, including for content.**
Measured against a non-bare receiver carrying that config plus
`denyNonFastforwards=true`: `git annex copy --to origin .` reports `ok` and the
object lands in the receiver's `.git/annex/objects`; `git push origin
HEAD:job-1` creates the branch; and a push into the receiver's *checked-out*
branch is accepted, with the worktree updated. Two consequences for the docs:
`updateInstead` refuses a push while the receiver's worktree is dirty, and
since jobs only ever push `job-*` refs, the checkout advances only when
`babs merge` pushes the main branch — the tree does not track individual jobs.

**Ref deletion is a hosting-policy risk once the endpoint is a forge.** Step 4
above replaces `git branch --delete` with
`git push --atomic --delete --force-with-lease=<ref>:<oid>`. Forges commonly
forbid ref deletion or non-fast-forward pushes, and `--atomic` means one
refusal fails the whole batch. On a non-local endpoint this must degrade to a
warning that leaves the merged branches in place; failing `babs merge` would
make such a project permanently unmergeable.

**`remote.<name>.annex-ignore` must be read, never forced.** The code currently
sets it to `false` unconditionally after creating the sibling. git-annex sets
that flag when its probe could not determine the remote's `annex.uuid`, which
covers two very different cases: a stale probe against a repository BABS itself
just `git annex init`-ed (clearing it is right), and a remote that genuinely has
no annex (clearing it re-creates exactly the silent `copy (notneeded)` content
loss described above). The design rule is: clear the flag only after positively
demonstrating the endpoint is an annex, and otherwise fail at `babs init`.

**`check-setup` should assert the content channel, not just the git channel.**
`git annex fsck --fast -f <content sibling>` in `analysis` — note `-f/--from`;
`--remote` is not a git-annex option — exercises uuid resolution and transport
setup, and fails loudly when the sibling cannot actually receive content. It
checks no data at that point (there are no results yet), which is the point: it
is a cheap positive assertion that the second channel is live, before jobs are
submitted. `merge.py` already runs the same command in `merge_ds` after
merging. The sibling name has to come from the provider, since it is
context-dependent (see the table above).

**Naming.** `--output-remote` should stay one option accepting either shape
(`metavar='PATH_OR_URL'`); naming it `…-path` would bake the narrowing into the
user-facing API. Internally the base attribute stays a `str` URL — a
`pathlib.Path` cannot hold `ssh://host/x` without mangling the `//` — and
`Path` belongs only inside the local providers, where the value genuinely
cannot be anything else. Help text and docstrings that said "path" were
accurately describing the narrowing, and were widened with the behaviour rather
than before it.

### Second round of review

**Creating a remote endpoint is possible for some transports.** The
create-locally/validate-remotely split above is too coarse: `datalad
create-sibling` creates *and* `git annex init`-s a repository over ssh, and
datalad ships per-forge helpers (`create-sibling-gin`, `-gitea`, `-gogs`,
`-gitlab`, `-github`). So the URL branch is really three-way — `ssh://`
creatable with `create-sibling`; a known forge creatable with its helper (which
needs credentials and host detection); anything else validate-only. Whether a
forge can hold annex content is a property of the forge, not the protocol
(forgejo-aneksajo and GIN can, plain Gitea and GitHub cannot), and the
"advertises `refs/heads/git-annex`" probe answers that without BABS keeping a
list. Out of scope for the first increment; recorded as the shape the URL
branch grows into.

**Verify that content arrived, per job, instead of inferring it at init.**
Measured, against a bare repository that was never annex-inited:

```
$ datalad push --to plain
action summary:
  copy (notneeded: 1)
  publish (ok: 2)          # refs published, content not, exit 0

$ git annex find --in here --not --in plain
f.txt                      # ← caught
```

After a successful `copy --to` against a properly annex-inited remote the same
command prints nothing. Two details: it exits 0 either way, so the test is on
*empty output*, not on the return code; and it reads the local location log,
which the push has just written, so it is a real check rather than a tautology.

This belongs in the job script, between the content push and the locked
result-ref push: if content is missing the job must **not** publish its result
branch, so it is counted as failed rather than silently producing an
unretrievable result. `merge.py:331` already runs `git annex find --not --in
<remote>` after merging; the job-time check is the missing one, and it is where
the damage starts. It also changes the argument in the previous section: the
init-time locality restriction exists because BABS cannot *infer* that a remote
endpoint will accept content, and a per-job proof that content landed is
strictly stronger than that inference. `datalad drop`/`remove` verifies
numcopies before purging on the same principle.

**Which provider a value selects should be detected, not spelled.** For an
endpoint that already exists, `ria-layout-version` identifies a RIA store and
`git rev-parse --is-bare-repository` separates a bare repository from one with
a worktree. That is authoritative, and it protects a user who points at a real
store whose directory name does not match any convention. A convention is only
needed to decide what to *create* at a path that does not exist yet, and there
the explicit form is better than sniffing a `.git` suffix off a bare path:
`ria+file:///…` for a RIA store, `file:///….git` for a bare repository,
`file:///…` for one with a worktree, a bare path keeping the RIA default.
Back-compatibility does not constrain this choice: `--output-remote` is new,
and a project that never passes it keeps its in-project `output_ria` untouched.

One measured caveat on the relative form: **`file:///./` is not a git URL.**
`git ls-remote "file://./bare.git"` fails with `'/bare.git' does not appear to
be a git repository` — git parses the `.` as the *host* and drops it — and
`file:///./bare.git` resolves to the absolute path `/./bare.git`. A relative
form therefore has to be a BABS-level convention expanded before anything
reaches git or datalad. That is fine (relative paths are already absolutized,
since git would otherwise resolve them against `analysis/`), but it must be
documented as a BABS convention rather than as URL syntax.

**A hosted endpoint needs documented prerequisites**, and one of them is not
about git at all:

1. an annex-capable forge (forgejo-aneksajo, GIN);
2. no branch protection on `job-*`;
3. ref deletion permitted — or BABS configured to leave merged branches in
   place (see the `--atomic` deletion note above);
4. **credentials on the compute nodes.** Jobs run unattended on cluster nodes,
   so pushing to a forge needs non-interactive authentication *there* — a
   deploy key, or a token in a credential helper. This is as hard a
   prerequisite as the annex one and has no counterpart in the local or RIA
   case, where the filesystem permissions of the submitting user are the whole
   story.

### Third round of review

**Use datalad's `RI` instead of hand-rolled URL classification.**
`datalad.support.network.RI` is a factory over resource identifiers with
`URL`, `SSHRI` and `PathRI` specializations, and it classifies every case this
implementation hand-rolled a rule for (measured, datalad 1.6.2):

| value | `RI(...)` |
|---|---|
| `/srv/out.git`, `out.git`, `./out.git`, `~/out.git` | `PathRI` |
| `file:///srv/out.git` | `URL`, scheme `file` |
| `ssh://user@host:2222/srv/x` | `URL`, scheme `ssh`, with username and port |
| `https://forge.org/u/r.git` | `URL`, scheme `https` |
| `git@host:out.git`, `user@host:/srv/x`, `host:/srv/x` | `SSHRI` — the case `urlparse` gets wrong |
| `ria+file:///srv/store`, `ria+ssh://host/srv/store` | `URL`, compound scheme preserved |

`.localpath` is the primitive worth having: it yields the path for `PathRI` and
for `file://` URLs and raises `ValueError` for anything else, which is the whole
local/non-local question in one property. `str(ri)` round-trips, so the user's
value can be stored and handed to git unchanged. The type maps directly onto
the dispatch above: `PathRI` and `file://` are local; `ria+*` is a RIA store;
`SSHRI` or `ssh://` is creatable with `datalad create-sibling`; `http(s)` is
validate-only or a forge helper.

For the RIA branch, `datalad.customremotes.ria_utils.verify_ria_url()` is worth
reusing rather than re-deriving: it validates the `ria+` prefix, restricts the
protocol to ssh/file/http(s), applies datalad's `rewrite_url()` config rewrites
(which BABS does not honour today), and returns `(host, base_path,
rewritten_url)` with `host=None` for `file` — a convention its own comment
marks as load-bearing for ORA.

Three things `RI` deliberately does not decide, so a thin BABS layer remains,
reduced to policy rather than parsing: `RI('')` is an empty `PathRI`;
`/srv/with space/out.git` is an accepted `PathRI`, while BABS must refuse
whitespace because the generated submission command would split it into
separate arguments; and `~`/relative paths are preserved literally, so
`expanduser`/`abspath` stay ours — which is the one boundary where
`pathlib.Path` belongs.

BABS tells local from remote in three separate hand-rolled places today, and
`RI` subsumes all of them: `output_remote._is_local_path()` (git's scp-style
rule plus `op.isabs`), `base.source_to_local_path()` (prefix-stripping
`ria+file://` / `file://` and splitting off a `#fragment`), and
`check_setup.py:194`, which pushes a symlink target through
`urlparse(...).path`. The middle one also has a latent bug that `RI` fixes for
free: it splits on `#` unconditionally, so a local path that legitimately
contains a `#` is truncated, whereas `RI('/srv/x#notafragment')` is a `PathRI`
whose path keeps the `#`, and `RI('ria+file:///srv/store#123-abc')` reports
`path='/srv/store'` with `fragment='123-abc'`. Note that `ria+file` does *not*
resolve through `.localpath` (the scheme is not `file`), so the RIA branch
either strips the `ria+` prefix or goes through `verify_ria_url()`.

`datalad.support.network` carries no `__all__` today, but datalad's maintainer
has confirmed it is intended as a public interface and will mark it as such, so
this is not a bet on an internal. (`datalad.api` is the wrong home for it
regardless: its 48 entries are 46 command functions, the `datalad` module, and
`Dataset` as the entry point *into* the command surface — `RI` is neither a
command nor a route to one, and a flat `datalad.api.URL` would be ambiguous at
every call site.)

One dependency to keep in view: the RIA branch wants
`datalad.customremotes.ria_utils.verify_ria_url()`, and `customremotes` reads
considerably more internal than `support`. If that one stays unmarked, BABS
hand-rolls `ria+` parsing anyway — the duplication this whole exercise is
trying to remove.

A thin adapter module is still worth having, but for the other reason: it is
the home for the checks that are BABS policy rather than URL semantics (empty
value, whitespace the generated submission command would split,
`expanduser`/`abspath`).

**Credentials are the user's; BABS's obligation is not to interfere.** Ssh
keys, kerberos and the like are site and user configuration, and no
`check-setup` probe on the submit host can meaningfully verify what a compute
node will have. What BABS owes is to leave that configuration intact, to keep
the door open for variables it may need to add later, and to document the
prerequisite.

It does not currently do the first part. `container.py:271` builds

```python
env_flags = '--export=DSLOCKFILE=' + babs.analysis_path + '/.SLURM_datalad_lock'
```

and Slurm documents `--export=<vars>` *without* `ALL` as propagating only the
named variables plus the `SLURM_*` set. Every BABS job therefore starts with no
`SSH_AUTH_SOCK`, no `KRB5CCNAME`, no site-set `GIT_SSH_COMMAND` and nothing a
module load placed in the environment. `--export=ALL,DSLOCKFILE=…` keeps the
submitting environment and still sets the variable BABS needs. This is
pre-existing behaviour affecting every project, not only remote endpoints, and
it is Slurm's documented semantics rather than something measured here — there
is no Slurm in the development container to demonstrate it, so it needs a real
check before the change lands.

### The dispatch as built

The four-way dispatch is implemented. What a value means, with an existing
target believed over its name:

| `--output-remote` | provider | BABS |
|---|---|---|
| omitted | `RiaOutputRemote` | creates the store in the project root |
| `/srv/store` | `RiaOutputRemote` | creates/uses a RIA store — a bare path stays RIA |
| `ria+file:///srv/store` | `RiaOutputRemote` | the same, said explicitly |
| `ria+ssh://host/srv/store` | `RiaOutputRemote` | datalad reaches the store over ssh |
| `file:///srv/out.git` | `BareGitOutputRemote` | creates/manages a bare repository |
| `file:///srv/out` | `WorktreeGitOutputRemote` | creates/manages one with a worktree |
| `ssh://…`, `user@host:…`, `https://…` | `RemoteGitOutputRemote` | validates; never creates |

Three things are worth recording about how it came out.

**Classification is `RI`'s, the kinds are BABS's.** `babs/resource.py` asks
`RI` what a value is and adds only policy — not empty, no whitespace, absolute
— plus `existing_kind()`, which reads the target itself (`ria-layout-version`,
`git rev-parse --is-bare-repository`) rather than inferring from the name. That
is what lets a store whose directory follows no convention still be used as
what it is.

**The second and third providers cost little because the first was factored.**
A shared `GitOutputRemote` holds the `annex-ignore` handling, the job
content-push command and the context-dependent sibling naming;
`WorktreeGitOutputRemote` is the `receive.*` configuration and its creation
rule, `RemoteGitOutputRemote` is validation and nothing else, ~40 and ~30 lines.
The `annex-ignore` positive-evidence check generalised exactly as its docstring
predicted: `annex.uuid` locally, the `git-annex` branch probe over a transport.

**What is verified.** All four providers run end to end in
`tests/e2e_output_remote.py`, each through `babs init` → `check-setup` → two
real participant jobs → `status` → `merge` → a fresh clone that retrieves the
results. The worktree receiver is additionally asserted to *show* the merged
results in place, which is the reason that provider exists.

The remote provider is exercised over a **real git transport**, not simulated:
`--ssh-host HOST` (a `Host` alias from the caller's ssh config) puts the whole
cycle over ssh — validation at init, both jobs pushing content and their result
branch, `babs merge` deleting the merged branches under the same OID lease, and
a closing `datalad clone ssh://…`. Without `--ssh-host` that provider is
skipped rather than faked with a path. Verified against a local `sshd`; a
`ria+ssh://` store is still uncovered.

**Where this could run.** `tests/e2e-slurm/` and `.circleci/config.yml` already
describe a containerised Slurm setup (`pennlinc/slurm-docker-ci`), and running
the suite inside that image is also what makes the `simbids` fixtures
available. Note that `.github/workflows/e2e-slurm.yml` is not a usable starting
point: besides being disabled (`branches-ignore: '**'`), it invokes
`tests/e2e-slurm/install-babs.sh` and `main.sh`, neither of which exists any
more. The ssh e2e above needs neither Slurm nor a container -- only an sshd,
which stock CI runners have.

## Testing

- **Unit** — the endpoint helpers are pure git plumbing over a URL, tested
  against `git init --bare` fixtures in `tmp_path` with no datalad: existence,
  head hash, branch enumeration, lease-safe deletion (from a cwd that is *not*
  a repository), and that an unreachable endpoint **raises** rather than
  returning `[]`. The providers are tested separately, including what
  `--output-remote` must refuse.
- **Regression** — pin Finding 3: an `ssh://user@host/path` push URL must not
  lose `user@host`.
- **Existing e2e unchanged** — `tests/e2e-slurm/` must pass identically through
  the minimal increment. `tests/test_check_setup.py:52,83` construct
  `ria+file://…#<id>` URLs (as helper input, not assertions) and will need
  updating once addressing changes.
- **Both providers end to end** — `tests/e2e_output_remote.py --provider both`
  runs the whole cycle (`babs init` → `check-setup` → real participant jobs →
  `babs merge` → a fresh consumer clone retrieving the merged results) against
  the RIA default and against a plain bare repository. It is not wired into CI
  (the `e2e-slurm` workflow is disabled upstream), so it is evidence rather
  than a regression gate — wiring it in is worth more than any further unit
  test here.
- **Not yet covered** — a non-local output remote end to end, which the branch
  does not support today. Once it does, the slurm test container can host an
  `ssh://localhost` repository, exercising both channels with no external
  infrastructure; and the non-bare `updateInstead` receiver deserves the same
  cycle, since its failure mode (a dirty worktree refusing a push) only shows
  up under a real merge.

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
5. **Two BABS projects pointing at one `--output-remote`.** Nothing detects
   it. Their `job-*` branches share a namespace and `babs merge` in either
   project would enumerate and delete the other's. A guard (record the project
   id in the endpoint, refuse a mismatch) is cheap; whether to make it an error
   or a warning is a policy call.
6. **Ref-deletion refusal on a hosted endpoint** — see above; needs a decision
   on warn-and-leave vs. an alternative namespace that forges allow deleting.
7. **Non-interference with job-side credentials.** Authentication on the
   compute nodes is the user's and the site's to arrange; BABS's part is to
   leave it alone and document it. The open item is therefore the
   `--export=ALL,DSLOCKFILE=…` change above -- which needs verifying on a real
   Slurm cluster -- plus deciding whether a hosted endpoint warrants a one-task
   canary job that only pushes an empty branch, rather than discovering the
   problem across a whole array.
