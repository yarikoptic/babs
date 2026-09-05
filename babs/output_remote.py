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

This module keeps that default byte-for-byte intact and adds a second
provider, in which a **plain bare git repository** carries both channels over
a single URL.  For annexed content to actually arrive in a plain bare
repository, the repository must itself be a git-annex repository -- a bare
repo that has never had ``git annex init`` run in it has no ``annex.uuid``,
so ``datalad push`` reports ``copy (notneeded)`` and silently transfers
nothing.  :meth:`BareGitOutputRemote.create_sibling` guarantees this.
"""

import os
import os.path as op
import subprocess
import warnings

from babs import resource

#: Name of the datalad sibling that receives the result *git refs*.
GIT_SIBLING_NAME = 'output'

#: Name that ``participant_job.sh`` gives the result git remote inside a job.
JOB_GIT_REMOTE_NAME = 'outputstore'

#: What ``participant_job.sh`` echoes/runs to publish content in a RIA project.
#: These strings reproduce the pre-existing template text exactly, so that the
#: default (RIA) ``participant_job.sh`` is unchanged.
RIA_CONTENT_PUSH_COMMENT = '# push result file content to output RIA storage:'
RIA_CONTENT_PUSH_ECHO = '# Push result file content to output RIA storage:'
RIA_CONTENT_PUSH_COMMAND = 'datalad push --to output-storage'

#: Name of the ORA special remote created by ``datalad create-sibling-ria``.
RIA_CONTENT_SIBLING = 'output-storage'


#: git-annex's own branch, which `git annex init` creates. It is never the
#: branch a reader of the results wants HEAD to point at.
_ANNEX_BRANCH = 'refs/heads/git-annex'


def _run_git_ok(args):
    """Run git, returning stripped stdout, or '' on any failure."""
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ''


class OutputRemote:
    """Base class: the place a BABS project publishes job results to."""

    #: Value recorded under ``output_remote.type`` in ``babs_proj_config.yaml``.
    type = None

    def __init__(self, url):
        self.url = str(url)

    def __repr__(self):
        return f'{type(self).__name__}({self.url!r})'

    # ------------------------------------------------------------------
    # `babs init`
    # ------------------------------------------------------------------
    def create_sibling(self, dataset, sibling_kwargs):
        """Create the ``output`` sibling (and its content channel) of `dataset`."""
        raise NotImplementedError

    def finalize(self, git_endpoint):
        """Run any post-``datalad push`` setup.  `git_endpoint` is the git URL."""

    # ------------------------------------------------------------------
    # addressing
    # ------------------------------------------------------------------
    def resolve_git_endpoint(self, recorded_push_url):
        """Turn the recorded ``output`` push URL into the git endpoint to use."""
        return recorded_push_url

    def clone_source(self, git_endpoint, analysis_dataset_id):
        """Return what ``datalad clone`` should be given to obtain the results."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # names/commands baked into generated code
    # ------------------------------------------------------------------
    @property
    def job_content_push_echo(self):
        """The ``echo`` line preceding the content push in ``participant_job.sh``."""
        raise NotImplementedError

    @property
    def job_content_push_command(self):
        """The shell command that publishes annexed content from a job."""
        raise NotImplementedError

    @property
    def merge_content_remote(self):
        """Name of the git-annex remote holding content, as seen from ``merge_ds``."""
        raise NotImplementedError

    @property
    def analysis_content_remote(self):
        """Name of the git-annex remote holding content, as seen from ``analysis``."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def to_config(self):
        """Return the ``output_remote`` mapping for ``babs_proj_config.yaml``.

        ``None`` means "do not record anything", which keeps the default
        project config identical to what BABS wrote before this feature and
        makes "key absent" unambiguously mean "the RIA default".
        """
        return


class RiaOutputRemote(OutputRemote):
    """The default: an output RIA store inside the BABS project root."""

    type = 'ria'

    def __init__(self, store_path):
        self.store_path = str(store_path)
        super().__init__('ria+file://' + self.store_path)

    def create_sibling(self, dataset, sibling_kwargs):
        dataset.create_sibling_ria(
            name=GIT_SIBLING_NAME,
            url=self.url,
            new_store_ok=True,
            **sibling_kwargs,
        )

    def resolve_git_endpoint(self, recorded_push_url):
        # If the recorded URL points at the RIA store root rather than at the
        # dataset directory (no `.git` there), resolve it through the
        # `alias/data` symlink, e.g. output_ria/alias/data -> XX/xxx-uuid.
        if not op.exists(op.join(recorded_push_url, '.git')):
            alias_link = op.join(self.store_path, 'alias', 'data')
            if op.exists(alias_link) and os.path.islink(alias_link):
                return op.realpath(alias_link)
        return recorded_push_url

    def finalize(self, git_endpoint):
        """Add the ``alias/data`` symlink into the output RIA store."""
        print("Adding an alias 'data' to output RIA store...")
        alias_dir = op.join(self.store_path, 'alias')
        if not op.exists(alias_dir):
            os.makedirs(alias_dir)
        the_symlink = op.join(alias_dir, 'data')
        if op.exists(the_symlink) & op.islink(the_symlink):
            # exists and is a symlink: remove first
            os.remove(the_symlink)
        os.symlink(git_endpoint, the_symlink)

    def clone_source(self, git_endpoint, analysis_dataset_id):
        return self.url + '#' + analysis_dataset_id

    @property
    def job_content_push_echo(self):
        return RIA_CONTENT_PUSH_ECHO

    @property
    def job_content_push_command(self):
        return RIA_CONTENT_PUSH_COMMAND

    @property
    def merge_content_remote(self):
        return RIA_CONTENT_SIBLING

    @property
    def analysis_content_remote(self):
        # The ORA remote is auto-enabled from the git-annex branch, so it
        # carries the same name in every clone.
        return RIA_CONTENT_SIBLING


class BareGitOutputRemote(OutputRemote):
    """A plain bare git repository that carries *both* publication channels.

    The same URL that receives the result branches also stores the annexed
    zip files, which is what makes this usable when the receiver is not the
    controller's own filesystem: there is no second, separately addressed
    content store whose URL could be recorded as a path that only the
    controller can reach.
    """

    type = 'bare-git'

    def __init__(self, url):
        super().__init__(url)
        repo_path = resource.usable_local_path(self.url)
        if repo_path is None:
            raise ValueError(
                f"'--output-remote {self.url}' cannot be used: "
                f'{resource.why_not_usable(self.url)} '
                'BABS can only guarantee that a repository is git-annex-initialized '
                '(and so able to receive result *content*) when it can reach it as a '
                'local path.'
            )
        self.repo_path = repo_path
        self.url = self.repo_path

    # ---------------- `babs init` ----------------
    def create_sibling(self, dataset, sibling_kwargs):
        shared = sibling_kwargs.get('shared')
        self._ensure_bare_repo(shared, sibling_kwargs.get('group'))
        self._ensure_annex()
        dataset.siblings(action='add', name=GIT_SIBLING_NAME, url=self.repo_path)
        self._clear_stale_annex_ignore(dataset.path)

    def _clear_stale_annex_ignore(self, dataset_path):
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

        # The positive check. For a local repository the annex uuid is
        # authoritative; a URL endpoint would instead have to advertise a
        # `git-annex` branch.
        if not self.annex_uuid():
            raise ValueError(
                f"git-annex marked the output remote '{self.repo_path}' as annex-ignore, "
                'and it has no annex.uuid -- so it cannot store result content, and every '
                'job would publish a result branch whose data is nowhere. Run '
                '`git annex init` in it, or point `--output-remote` elsewhere.'
            )
        warnings.warn(
            f"remote.{GIT_SIBLING_NAME}.annex-ignore was set to 'true' for "
            f"'{self.repo_path}', which is a git-annex repository -- git-annex's probe "
            'must have failed transiently. Clearing it, so result content can be pushed.',
            stacklevel=2,
        )
        subprocess.run(
            ['git', 'config', f'remote.{GIT_SIBLING_NAME}.annex-ignore', 'false'],
            cwd=dataset_path,
            check=True,
        )

    def _ensure_bare_repo(self, shared=None, group=None):
        """Create the bare repository if needed; validate it if it exists."""
        if op.exists(self.repo_path):
            # Check this before anything shells out with cwd=repo_path: Popen
            # raises NotADirectoryError on a file, which would surface as a
            # traceback instead of the message below.
            if not op.isdir(self.repo_path):
                raise ValueError(
                    f"'--output-remote {self.repo_path}' exists but is a file, not a directory."
                )
            if self._is_bare_repo():
                return
            if not op.isdir(self.repo_path):
                raise ValueError(
                    f"'--output-remote {self.repo_path}' exists but is a file, not a directory."
                )
            if os.listdir(self.repo_path):
                # Either a non-bare repo (which would refuse pushes to its
                # checked-out branch) or somebody's data -- a typo in
                # `--output-remote` must not scatter git internals into it.
                raise ValueError(
                    f"'--output-remote {self.repo_path}' exists but is not a bare git "
                    'repository, and is not empty. Point `--output-remote` at a bare '
                    'repository (`git init --bare`), an empty directory, or a path '
                    'that does not exist yet.'
                )
            # An existing empty directory is fine: initialise into it.
        os.makedirs(op.dirname(self.repo_path), exist_ok=True)
        cmd = ['git', 'init', '--bare']
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

    def _is_bare_repo(self):
        proc = subprocess.run(
            ['git', 'rev-parse', '--is-bare-repository'],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0 and proc.stdout.strip() == 'true'

    def annex_uuid(self):
        """Return the repository's ``annex.uuid``, or ``''`` if it has none."""
        proc = subprocess.run(
            ['git', 'config', '--get', 'annex.uuid'],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ''

    def _ensure_annex(self):
        """Make the bare repository a git-annex repository.

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
                'this bare repository cannot receive result content.'
            )

    # ---------------- addressing ----------------
    def finalize(self, git_endpoint):
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

    def clone_source(self, git_endpoint, analysis_dataset_id):
        return git_endpoint

    # ---------------- generated code ----------------
    @property
    def job_content_push_echo(self):
        return '# Push result file content to the output remote:'

    @property
    def job_content_push_command(self):
        # Content only: the result branch is pushed separately, and last, so
        # that a visible result branch always implies retrievable content.
        # `--in here` restricts the transfer to content this job actually has.
        return f'git annex copy --to {JOB_GIT_REMOTE_NAME} --in here .'

    @property
    def merge_content_remote(self):
        # `merge_ds` is a plain clone of the bare repository, so the remote
        # that holds the content is `origin` itself.
        return 'origin'

    @property
    def analysis_content_remote(self):
        # One sibling carries both channels here, so content lives on the
        # same remote as the refs.
        return GIT_SIBLING_NAME

    def to_config(self):
        return {'type': self.type, 'url': self.repo_path}


def make_output_remote(output_ria_path, output_remote=None):
    """Build the output remote provider for a BABS project.

    Parameters
    ----------
    output_ria_path : str
        Path of the in-project output RIA store (used by the default).
    output_remote : str or None
        Value of ``babs init --output-remote``.  ``None`` selects the default
        RIA behaviour.
    """
    if output_remote is None:
        return RiaOutputRemote(output_ria_path)
    return BareGitOutputRemote(output_remote)


def output_remote_from_config(output_ria_path, config_section):
    """Rebuild the provider from ``babs_proj_config.yaml``.

    A project created before ``--output-remote`` existed has no
    ``output_remote`` section; that is the RIA default.
    """
    if not config_section:
        return RiaOutputRemote(output_ria_path)
    if not isinstance(config_section, dict):
        raise TypeError(
            f"'output_remote' in babs_proj_config.yaml must be a mapping, "
            f'got {type(config_section).__name__}'
        )
    remote_type = config_section.get('type', RiaOutputRemote.type)
    if remote_type == RiaOutputRemote.type:
        return RiaOutputRemote(output_ria_path)
    if remote_type == BareGitOutputRemote.type:
        url = config_section.get('url')
        if not url:
            raise ValueError(
                "'output_remote' of type 'bare-git' in babs_proj_config.yaml is missing its 'url'."
            )
        return BareGitOutputRemote(url)
    raise ValueError(
        f"Unknown 'output_remote.type' in babs_proj_config.yaml: {remote_type!r}. "
        f"Known types: '{RiaOutputRemote.type}', '{BareGitOutputRemote.type}'."
    )
