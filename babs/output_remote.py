"""Where a BABS project publishes the results of its jobs.

Publication has **two channels**, and both of them have to reach the same
place for results to be usable:

1. *git refs* -- one ``job-<id>-<task>-<sub>`` branch per finished job, holding
   the provenance and the annex pointers.  ``participant_job.sh`` pushes this
   last, under ``flock``, as the completion marker of the job.
2. *git-annex content* -- the actual zip files.  ``participant_job.sh`` pushes
   this **first**, so that a visible result branch always implies retrievable
   content.

For the historical (and default) RIA layout the two channels are separate
endpoints: a bare git repository inside the store for the refs, and an ORA
special remote (recorded in the ``git-annex`` branch with ``autoenable=true``)
for the content.

Both channels are published the same way whatever the receiver is: the
content with ``git annex copy --to <sibling>``, then the result branch with
``git push``, under ``flock``. Only the sibling *name* differs between
receivers, and that is configuration rather than a different command.

For annexed content to actually arrive, the receiver must itself be a
git-annex repository: one that has never had ``git annex init`` run in it has
no ``annex.uuid``, so ``datalad push`` reports ``copy (notneeded)`` and
silently transfers nothing. The local providers guarantee that by initializing
it; :class:`RemoteGitOutputRemote` validates it instead.
"""

import os
import os.path as op
import subprocess
import warnings
from collections.abc import Sequence
from typing import Any, ClassVar

from babs import resource
from babs.git_endpoint import GitEndpointError, ls_remote_heads

#: Name of the datalad sibling that receives the result *git refs*.
GIT_SIBLING_NAME = 'output'

#: Name that ``participant_job.sh`` gives the result git remote inside a job.
JOB_GIT_REMOTE_NAME = 'outputstore'

#: Name of the ORA special remote created by ``datalad create-sibling-ria``.
RIA_CONTENT_SIBLING = 'output-storage'


#: git-annex's own branch, which `git annex init` creates. It is never the
#: branch a reader of the results wants HEAD to point at, and its presence
#: at an endpoint is what says the endpoint is a git-annex repository.
_ANNEX_BRANCH_NAME = 'git-annex'
_ANNEX_BRANCH = 'refs/heads/' + _ANNEX_BRANCH_NAME


def _run_git_ok(args: Sequence[str]) -> str:
    """Run git, returning stripped stdout, or '' on any failure."""
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ''


class OutputRemote:
    """Base class: the place a BABS project publishes job results to."""

    #: Short name of the kind of receiver, for messages and tests. It is not
    #: recorded in the project config: the URL says which provider it is.
    type: ClassVar[str] = ''

    def __init__(self, url: str) -> None:
        self.url = str(url)

    def __repr__(self) -> str:
        return f'{type(self).__name__}({self.url!r})'

    # ------------------------------------------------------------------
    # `babs init`
    # ------------------------------------------------------------------
    def create_sibling(self, dataset: Any, sibling_kwargs: dict[str, Any]) -> None:
        """Create the ``output`` sibling (and its content channel) of `dataset`."""
        raise NotImplementedError

    def finalize(self, git_endpoint: str) -> None:
        """Run any post-``datalad push`` setup.  `git_endpoint` is the git URL."""

    # ------------------------------------------------------------------
    # addressing
    # ------------------------------------------------------------------
    def resolve_git_endpoint(self, recorded_push_url: str) -> str:
        """Turn the recorded ``output`` push URL into the git endpoint to use."""
        return recorded_push_url

    def clone_source(self, git_endpoint: str, analysis_dataset_id: str) -> str:
        """Return what ``datalad clone`` should be given to obtain the results."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # names/commands baked into generated code
    # ------------------------------------------------------------------
    @property
    def job_content_remote(self) -> str:
        """Name of the git-annex remote a *job* pushes result content to."""
        raise NotImplementedError

    @property
    def merge_content_remote(self) -> str:
        """Name of the git-annex remote holding content, as seen from ``merge_ds``."""
        raise NotImplementedError

    @property
    def analysis_content_remote(self) -> str:
        """Name of the git-annex remote holding content, as seen from ``analysis``."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    @property
    def config_url(self) -> str:
        """The value to record, which must select this provider when re-read.

        Only the URL is stored: which provider it means is derivable from it,
        so recording a `type` beside it would be a second source of truth that
        can disagree with the first.
        """
        return self.url

    def to_config(self) -> dict[str, str] | None:
        """Return the ``output_remote`` mapping for ``babs_proj_config.yaml``.

        ``None`` means "do not record anything", which keeps the default
        project config identical to what BABS wrote before this feature and
        makes "key absent" unambiguously mean "the RIA default".
        """
        return None


class RiaOutputRemote(OutputRemote):
    """A RIA store: the project's own by default, or one named explicitly.

    Accepts a filesystem path, a ``ria+file://`` URL, or a ``ria+ssh://`` (or
    http(s)) URL for a store datalad reaches over that transport. Only a local
    store has a ``store_path``; for a remote one datalad does the work and
    BABS does not touch the store's layout itself.
    """

    type = 'ria'

    def __init__(self, store: str, in_project: bool = False) -> None:
        store = str(store)
        #: The in-project default is not recorded in the project config, so
        #: that "no `output_remote` section" keeps meaning exactly that.
        self.in_project = in_project
        if resource.is_ria(store):
            url = store
            self.store_path = resource.local_path(store)
        else:
            path = resource.usable_local_path(store)
            if path is None:
                raise ValueError(
                    f"'--output-remote {store}' cannot be used: {resource.why_not_usable(store)}"
                )
            self.store_path = path
            url = 'ria+file://' + path
        super().__init__(url)

    def create_sibling(self, dataset: Any, sibling_kwargs: dict[str, Any]) -> None:
        if self.store_path is None:
            # The `alias/data` symlink `finalize` writes for a local store has
            # to be created by datalad, over the same transport it uses to
            # reach the store.
            sibling_kwargs = dict(sibling_kwargs, alias='data')
        dataset.create_sibling_ria(
            name=GIT_SIBLING_NAME,
            url=self.url,
            new_store_ok=True,
            **sibling_kwargs,
        )

    def to_config(self) -> dict[str, str] | None:
        return None if self.in_project else {'url': self.config_url}

    def resolve_git_endpoint(self, recorded_push_url: str) -> str:
        # If the recorded URL points at the RIA store root rather than at the
        # dataset directory (no `.git` there), resolve it through the
        # `alias/data` symlink, e.g. output_ria/alias/data -> XX/xxx-uuid.
        if self.store_path is None:
            return recorded_push_url
        if not op.exists(op.join(recorded_push_url, '.git')):
            alias_link = op.join(self.store_path, 'alias', 'data')
            if op.exists(alias_link) and os.path.islink(alias_link):
                return op.realpath(alias_link)
        return recorded_push_url

    def finalize(self, git_endpoint: str) -> None:
        """Add the ``alias/data`` symlink into the output RIA store."""
        if self.store_path is None:
            return  # created by datalad at `create_sibling` time
        print("Adding an alias 'data' to output RIA store...")
        alias_dir = op.join(self.store_path, 'alias')
        if not op.exists(alias_dir):
            os.makedirs(alias_dir)
        the_symlink = op.join(alias_dir, 'data')
        if op.exists(the_symlink) & op.islink(the_symlink):
            # exists and is a symlink: remove first
            os.remove(the_symlink)
        os.symlink(git_endpoint, the_symlink)

    def clone_source(self, git_endpoint: str, analysis_dataset_id: str) -> str:
        return self.url + '#' + analysis_dataset_id

    @property
    def job_content_remote(self) -> str:
        # The ORA special remote is auto-enabled from the git-annex branch, so
        # a job clone resolves it by this name without configuring anything.
        return RIA_CONTENT_SIBLING

    @property
    def merge_content_remote(self) -> str:
        return RIA_CONTENT_SIBLING

    @property
    def analysis_content_remote(self) -> str:
        # The ORA remote is auto-enabled from the git-annex branch, so it
        # carries the same name in every clone.
        return RIA_CONTENT_SIBLING


class GitOutputRemote(OutputRemote):
    """A git repository that carries *both* publication channels.

    The same URL that receives the result branches also stores the annexed
    zip files, which is what makes this usable when the receiver is not the
    controller's own filesystem: there is no second, separately addressed
    content store whose URL could be recorded as a path that only the
    controller can reach.
    """

    # ---------------- `babs init` ----------------
    def _annex_evidence(self) -> bool:
        """Positive evidence that the endpoint really is a git-annex repository."""
        raise NotImplementedError

    def _clear_stale_annex_ignore(self, dataset_path: str) -> None:
        """Clear `annex-ignore` only when the endpoint provably *is* an annex.

        git-annex sets ``remote.<name>.annex-ignore=true`` when its probe could
        not determine the remote's ``annex.uuid``, and caches that so it stops
        retrying. Two very different situations reach that flag:

        * a stale probe against a repository BABS has just ``git annex init``-ed
          -- clearing it is right; and
        * a remote that genuinely has no annex -- where clearing it would
          replace a condition git-annex correctly detected with the silent
          failure this whole provider exists to avoid (``datalad push``
          reporting ``copy (notneeded)``, zero objects, exit 0).

        Setting it to ``false`` unconditionally cannot tell those apart, so the
        flag is read first and only ever cleared against positive evidence of
        an annex. The RIA default is the reminder that ``annex-ignore=true`` is
        often entirely correct: its *git* sibling carries no content, which
        goes to the ORA remote instead.
        """
        proc = subprocess.run(
            ['git', 'config', '--type=bool', '--get', f'remote.{GIT_SIBLING_NAME}.annex-ignore'],
            cwd=dataset_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or proc.stdout.strip() != 'true':
            return  # unset or already false: git-annex's own view is fine

        if not self._annex_evidence():
            raise ValueError(
                f"git-annex marked the output remote '{self.url}' as annex-ignore, and it "
                'is not a git-annex repository -- so it cannot store result content, and '
                'every job would publish a result branch whose data is nowhere. Run '
                '`git annex init` in it, or point `--output-remote` elsewhere.'
            )
        warnings.warn(
            f"remote.{GIT_SIBLING_NAME}.annex-ignore was set to 'true' for "
            f"'{self.url}', which is a git-annex repository -- git-annex's probe "
            'must have failed transiently. Clearing it, so result content can be pushed.',
            stacklevel=2,
        )
        subprocess.run(
            ['git', 'config', f'remote.{GIT_SIBLING_NAME}.annex-ignore', 'false'],
            cwd=dataset_path,
            check=True,
        )

    # ---------------- addressing ----------------
    def finalize(self, git_endpoint: str) -> None:
        """Nothing to do: a repository with a worktree already has a `HEAD`."""

    def clone_source(self, git_endpoint: str, analysis_dataset_id: str) -> str:
        return git_endpoint

    # ---------------- generated code ----------------
    @property
    def job_content_remote(self) -> str:
        # One sibling carries both channels, so content goes to the same
        # remote the job script adds for the result branch.
        return JOB_GIT_REMOTE_NAME

    @property
    def merge_content_remote(self) -> str:
        # `merge_ds` is a plain clone of the repository, so the remote that
        # holds the content is `origin` itself.
        return 'origin'

    @property
    def analysis_content_remote(self) -> str:
        # One sibling carries both channels here, so content lives on the
        # same remote as the refs.
        return GIT_SIBLING_NAME

    def to_config(self) -> dict[str, str] | None:
        return {'url': self.config_url}


class LocalGitOutputRemote(GitOutputRemote):
    """A repository on this filesystem, which BABS creates and manages."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        repo_path = resource.usable_local_path(self.url)
        if repo_path is None:
            raise ValueError(
                f"'--output-remote {self.url}' cannot be used: {resource.why_not_usable(self.url)}"
            )
        self.repo_path = repo_path
        self.url = self.repo_path

    def create_sibling(self, dataset: Any, sibling_kwargs: dict[str, Any]) -> None:
        self._ensure_repo(sibling_kwargs.get('shared'), sibling_kwargs.get('group'))
        self._ensure_annex()
        dataset.siblings(action='add', name=GIT_SIBLING_NAME, url=self.repo_path)
        self._clear_stale_annex_ignore(dataset.path)

    @property
    def config_url(self) -> str:
        # `self.url` is the plain path here, which on re-reading would mean a
        # RIA store; `file://` is what says "a git repository at this path".
        return 'file://' + self.repo_path

    def _ensure_repo(self, shared: str | None = None, group: str | None = None) -> None:
        """Create the repository if needed; validate it if it exists."""
        raise NotImplementedError

    def _init_repo(self, extra_args: Sequence[str], shared: str | None, group: str | None) -> None:
        """`git init` the repository, then apply the shared-group ownership."""
        os.makedirs(op.dirname(self.repo_path), exist_ok=True)
        cmd = ['git', 'init', *extra_args]
        if shared:
            cmd.append(f'--shared={shared}')
        cmd.append(self.repo_path)
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        if group:
            # `--shared=group` only sets the permission bits; the tree is still
            # owned by the creating user's *primary* group, and the setgid bit
            # then propagates that wrong group to everything git-annex writes
            # under annex/objects/. create_sibling_ria applies the group for a
            # RIA store; do the same here, or a second member's job cannot
            # write its results.
            proc = subprocess.run(
                ['chgrp', '-R', group, self.repo_path],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                raise ValueError(
                    f"Could not set group '{group}' on the output remote "
                    f"'{self.repo_path}': {proc.stderr.strip()}"
                )

    def _refuse_if_unusable_directory(self) -> None:
        """Common checks before anything shells out with ``cwd=repo_path``.

        `Popen` raises `NotADirectoryError` on a file, which would surface as
        a traceback rather than a message naming what is wrong.
        """
        if op.exists(self.repo_path) and not op.isdir(self.repo_path):
            raise ValueError(
                f"'--output-remote {self.repo_path}' exists but is a file, not a directory."
            )

    def _existing_kind(self) -> str | None:
        return resource.existing_kind(self.repo_path)

    def annex_uuid(self) -> str:
        """Return the repository's ``annex.uuid``, or ``''`` if it has none."""
        proc = subprocess.run(
            ['git', 'config', '--get', 'annex.uuid'],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ''

    def _annex_evidence(self) -> bool:
        return bool(self.annex_uuid())

    def _ensure_annex(self) -> None:
        """Make the repository a git-annex repository.

        Without this, the repository has no ``annex.uuid``; git-annex then
        treats it as a git-only remote and *silently* transfers no content
        (``datalad push`` reports ``copy (notneeded)``), so every job would
        publish a result branch whose zip file is nowhere.
        """
        if self.annex_uuid():
            return
        subprocess.run(
            ['git', 'annex', 'init', 'babs-output'],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        if not self.annex_uuid():
            raise RuntimeError(
                f"`git annex init` in '{self.repo_path}' did not set annex.uuid; "
                'this repository cannot receive result content.'
            )


class BareGitOutputRemote(LocalGitOutputRemote):
    """A plain bare git repository: `--output-remote file:///srv/out.git`."""

    type = 'bare-git'

    def _ensure_repo(self, shared: str | None = None, group: str | None = None) -> None:
        self._refuse_if_unusable_directory()
        if op.isdir(self.repo_path):
            kind = self._existing_kind()
            if kind == 'bare':
                return
            if kind is not None or os.listdir(self.repo_path):
                # A repository with a worktree (which would refuse pushes to
                # its checked-out branch), a RIA store, or somebody's data --
                # a typo in `--output-remote` must not scatter git internals
                # into it.
                raise ValueError(
                    f"'--output-remote {self.repo_path}' exists but is not a bare git "
                    'repository, and is not empty. Point `--output-remote` at a bare '
                    'repository (`git init --bare`), an empty directory, or a path '
                    'that does not exist yet.'
                )
            # An existing empty directory is fine: initialise into it.
        self._init_repo(['--bare'], shared, group)

    def finalize(self, git_endpoint: str) -> None:
        """Point the bare repository's ``HEAD`` at the branch BABS publishes.

        ``git init --bare`` takes ``HEAD`` from the creating machine's
        ``init.defaultBranch``, and pushing a differently named branch does not
        update it -- nor does BABS create the repository at all when the user
        points ``--output-remote`` at a pre-existing one. A dangling ``HEAD``
        then makes ``git ls-remote <url> HEAD`` return nothing (with exit 0),
        so ``babs check-setup`` reports the endpoint as empty when it is fully
        populated, and ``git remote show`` reports ``HEAD branch: (unknown)``,
        which stops ``babs merge`` outright.
        """
        head = _run_git_ok(['git', '-C', self.repo_path, 'symbolic-ref', '--quiet', 'HEAD'])
        if head and _run_git_ok(
            ['git', '-C', self.repo_path, 'rev-parse', '--verify', '--quiet', head]
        ):
            return  # HEAD already resolves; leave it alone
        for ref in _run_git_ok(
            ['git', '-C', self.repo_path, 'for-each-ref', '--format=%(refname)', 'refs/heads/']
        ).splitlines():
            if ref != f'{_ANNEX_BRANCH}' and ref.strip():
                subprocess.run(
                    ['git', '-C', self.repo_path, 'symbolic-ref', 'HEAD', ref.strip()],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return


class WorktreeGitOutputRemote(LocalGitOutputRemote):
    """A regular repository with a worktree: `--output-remote file:///srv/out`.

    A repository with a checked-out branch normally refuses a push into it.
    ``receive.denyCurrentBranch=updateInstead`` makes it accept one and update
    its working tree with it, which is what makes the results *readable in
    place* rather than only through a clone. Two things follow from that and
    are worth knowing: the push is refused while the worktree is dirty, and
    since jobs only ever push ``job-*`` branches, the checkout advances when
    ``babs merge`` pushes the main branch, not per job.
    """

    type = 'worktree-git'

    #: Pushes into this repository must not rewrite history, and must update
    #: the worktree rather than being refused for touching a checked-out branch.
    RECEIVE_CONFIG: ClassVar[dict[str, str]] = {
        'receive.denyNonFastforwards': 'true',
        'receive.denyCurrentBranch': 'updateInstead',
    }

    def _ensure_repo(self, shared: str | None = None, group: str | None = None) -> None:
        self._refuse_if_unusable_directory()
        kind = self._existing_kind() if op.isdir(self.repo_path) else None
        if kind is None:
            if op.isdir(self.repo_path) and os.listdir(self.repo_path):
                raise ValueError(
                    f"'--output-remote {self.repo_path}' exists but is not a git "
                    'repository, and is not empty. Point `--output-remote` at a git '
                    'repository, an empty directory, or a path that does not exist yet.'
                )
            self._init_repo([], shared, group)
        elif kind != 'worktree':
            raise ValueError(
                f"'--output-remote {self.repo_path}' is an existing {kind} repository, "
                "but was given as a plain `file://` path, which means 'a repository with "
                "a worktree'. Add a `.git` suffix for a bare repository, or use "
                'a `ria+file://` URL for a RIA store.'
            )
        for key, value in self.RECEIVE_CONFIG.items():
            subprocess.run(
                ['git', '-C', self.repo_path, 'config', key, value],
                capture_output=True,
                text=True,
                check=True,
            )


class RemoteGitOutputRemote(GitOutputRemote):
    """A git repository BABS cannot reach as a path: ssh, https, a forge.

    BABS never creates one. It cannot run ``git annex init`` over a git
    transport, and a repository that is not annex-initialized accepts the
    result *branches* while silently dropping the result *content*
    (``datalad push`` reports ``copy (notneeded)``, transfers zero objects and
    exits 0). So the endpoint is validated instead: it must be reachable, and
    it must advertise a ``git-annex`` branch, which is what distinguishes an
    annex-capable receiver (forgejo-aneksajo, GIN, or any repository someone
    ran ``git annex init`` in) from a plain git host.
    """

    type = 'remote-git'

    def create_sibling(self, dataset: Any, sibling_kwargs: dict[str, Any]) -> None:
        self._validate_endpoint()
        dataset.siblings(action='add', name=GIT_SIBLING_NAME, url=self.url)
        self._clear_stale_annex_ignore(dataset.path)

    def _validate_endpoint(self) -> None:
        try:
            heads = ls_remote_heads(self.url)
        except GitEndpointError as exc:
            raise ValueError(
                f"'--output-remote {self.url}' cannot be reached: {exc}\n"
                'BABS does not create repositories over a git transport. Create it '
                'there, run `git annex init` in it, and try again.'
            ) from exc
        if _ANNEX_BRANCH_NAME not in heads:
            raise ValueError(
                f"'--output-remote {self.url}' is reachable but does not advertise a "
                "'git-annex' branch, so it is not a git-annex repository. Result "
                '*content* would silently not transfer, leaving every job with a '
                'result branch whose data is nowhere. Run `git annex init` in it '
                '(the host must support git-annex, e.g. forgejo-aneksajo or GIN).'
            )

    def _annex_evidence(self) -> bool:
        try:
            return _ANNEX_BRANCH_NAME in ls_remote_heads(self.url)
        except GitEndpointError:
            return False


def make_output_remote(output_ria_path: str, output_remote: str | None = None) -> OutputRemote:
    """Build the output remote provider for a BABS project.

    The value's shape says what kind of endpoint it is, and an existing target
    is believed over its name:

    ============================== =========================================
    ``--output-remote``            provider
    ============================== =========================================
    omitted                        RIA store inside the project root
    ``/srv/store``                 RIA store there (a bare path stays RIA)
    ``ria+file:///srv/store``      the same, said explicitly
    ``ria+ssh://host/srv/store``   a RIA store datalad reaches over ssh
    ``file:///srv/out.git``        a bare git repository BABS manages
    ``file:///srv/out``            a git repository with a worktree
    ``ssh://…``, ``git@host:…``    an existing repository, validated not created
    ============================== =========================================

    Parameters
    ----------
    output_ria_path : str
        Path of the in-project output RIA store (used by the default).
    output_remote : str or None
        Value of ``babs init --output-remote``.  ``None`` selects the default
        RIA behaviour.
    """
    if output_remote is None:
        return RiaOutputRemote(output_ria_path, in_project=True)

    if resource.is_ria(output_remote):
        return RiaOutputRemote(output_remote)

    if resource.local_path(output_remote) is None:
        return RemoteGitOutputRemote(output_remote)

    path = resource.usable_local_path(output_remote)
    if path is None:
        raise ValueError(
            f"'--output-remote {output_remote}' cannot be used: "
            f'{resource.why_not_usable(output_remote)}'
        )

    kind = resource.existing_kind(path)
    if kind is None:
        # Nothing there yet, so the way it was written decides. A bare path
        # keeps meaning a RIA store, which is what BABS has always created.
        if not resource.is_file_url(output_remote):
            kind = 'ria'
        else:
            kind = 'bare' if path.endswith('.git') else 'worktree'
    providers: dict[str, type[OutputRemote]] = {
        'ria': RiaOutputRemote,
        'bare': BareGitOutputRemote,
        'worktree': WorktreeGitOutputRemote,
    }
    return providers[kind](path)


def output_remote_from_config(
    output_ria_path: str, config_section: dict[str, Any] | None
) -> OutputRemote:
    """Rebuild the provider from ``babs_proj_config.yaml``.

    A project created before ``--output-remote`` existed has no
    ``output_remote`` section; that is the RIA default. Otherwise the section
    carries a ``url``, and which provider it means is derived from it exactly
    as it was at ``babs init`` -- there is no second, independently stored
    answer that could disagree.
    """
    if not config_section:
        return RiaOutputRemote(output_ria_path, in_project=True)
    if not isinstance(config_section, dict):
        raise TypeError(
            f"'output_remote' in babs_proj_config.yaml must be a mapping, "
            f'got {type(config_section).__name__}'
        )
    url = config_section.get('url')
    if not url:
        raise ValueError("'output_remote' in babs_proj_config.yaml is missing its 'url'.")
    remote = make_output_remote(output_ria_path, url)
    # `type` is redundant, so it is no longer written -- but if a hand-edited
    # config carries one, disagreeing with the URL is a mistake worth naming
    # rather than silently ignoring.
    recorded_type = config_section.get('type')
    if recorded_type is not None and recorded_type != remote.type:
        raise ValueError(
            f"'output_remote' in babs_proj_config.yaml says type {recorded_type!r}, but its "
            f"url {url!r} is a {remote.type!r} endpoint. Remove the 'type' key: it is "
            'derived from the url.'
        )
    return remote
