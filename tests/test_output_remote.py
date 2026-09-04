"""Unit tests for the output-remote providers.

Everything here runs against real `git init --bare` fixtures in `tmp_path`;
no datalad, no cluster, no network.
"""

import os
import subprocess

import pytest

from babs.git_endpoint import list_result_branches
from babs.output_remote import (
    BareGitOutputRemote,
    RiaOutputRemote,
    make_output_remote,
    output_remote_from_config,
)


def _git(*args, cwd=None, check=True):
    return subprocess.run(
        ['git', *args], cwd=cwd, capture_output=True, text=True, check=check
    ).stdout.strip()


class TestSelection:
    def test_default_is_ria(self, tmp_path):
        remote = make_output_remote(str(tmp_path / 'output_ria'))
        assert isinstance(remote, RiaOutputRemote)
        assert remote.type == 'ria'
        assert remote.url == 'ria+file://' + str(tmp_path / 'output_ria')

    def test_explicit_path_selects_bare_git(self, tmp_path):
        remote = make_output_remote(str(tmp_path / 'output_ria'), str(tmp_path / 'out.git'))
        assert isinstance(remote, BareGitOutputRemote)
        assert remote.type == 'bare-git'

    def test_a_project_predating_this_feature_gets_the_ria_default(self, tmp_path):
        """An existing project's babs_proj_config.yaml has no `output_remote`."""
        for absent in (None, {}):
            remote = output_remote_from_config(str(tmp_path / 'output_ria'), absent)
            assert isinstance(remote, RiaOutputRemote)

    def test_round_trips_through_config(self, tmp_path):
        bare = tmp_path / 'out.git'
        remote = make_output_remote(str(tmp_path / 'output_ria'), str(bare))
        cfg = remote.to_config()
        assert cfg == {'type': 'bare-git', 'url': str(bare)}
        rebuilt = output_remote_from_config(str(tmp_path / 'output_ria'), cfg)
        assert isinstance(rebuilt, BareGitOutputRemote)
        assert rebuilt.repo_path == str(bare)

    def test_ria_default_is_not_recorded_in_the_project_config(self, tmp_path):
        assert make_output_remote(str(tmp_path / 'output_ria')).to_config() is None

    def test_unknown_type_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match='Unknown'):
            output_remote_from_config(str(tmp_path), {'type': 'sftp'})

    def test_bare_git_without_url_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match='url'):
            output_remote_from_config(str(tmp_path), {'type': 'bare-git'})

    def test_non_local_url_is_refused_loudly(self, tmp_path):
        # BABS cannot guarantee `git annex init` on the far side, and a bare
        # repo without an annex silently drops result *content*.
        with pytest.raises(ValueError, match='local path'):
            BareGitOutputRemote('ssh://user@host/srv/out.git')


class TestBareGitCreation:
    def test_creates_a_bare_repo_that_can_hold_annexed_content(self, tmp_path):
        bare = tmp_path / 'nested' / 'out.git'
        remote = BareGitOutputRemote(str(bare))
        remote._ensure_bare_repo()
        remote._ensure_annex()

        assert (bare / 'HEAD').exists()
        assert _git('rev-parse', '--is-bare-repository', cwd=bare) == 'true'
        # The load-bearing bit: without an annex.uuid, git-annex treats the
        # repo as git-only and transfers no content at all.
        assert remote.annex_uuid() != ''
        assert 'git-annex' in _git('branch', '--list', cwd=bare)

    def test_accepts_a_pre_existing_bare_repo(self, tmp_path):
        bare = tmp_path / 'out.git'
        _git('init', '--bare', str(bare))
        remote = BareGitOutputRemote(str(bare))
        remote._ensure_bare_repo()
        remote._ensure_annex()
        assert remote.annex_uuid() != ''

    def test_annex_init_is_idempotent(self, tmp_path):
        bare = tmp_path / 'out.git'
        remote = BareGitOutputRemote(str(bare))
        remote._ensure_bare_repo()
        remote._ensure_annex()
        first = remote.annex_uuid()
        remote._ensure_annex()
        assert remote.annex_uuid() == first

    def test_accepts_an_existing_empty_directory(self, tmp_path):
        """An empty directory is a fine place to `git init --bare` into; only a
        directory holding something else must be refused."""
        empty = tmp_path / 'plain'
        empty.mkdir()
        remote = BareGitOutputRemote(str(empty))
        remote._ensure_bare_repo()
        assert remote._is_bare_repo()

    def test_rejects_a_working_tree_repo(self, tmp_path):
        work = tmp_path / 'work'
        _git('init', str(work))
        with pytest.raises(ValueError, match='not a bare git repository'):
            BareGitOutputRemote(str(work))._ensure_bare_repo()


class TestPublicationContract:
    """Both channels must reach the endpoint, and refs must go last."""

    def test_ria_job_stanza_is_the_historical_text(self, tmp_path):
        remote = RiaOutputRemote(str(tmp_path / 'output_ria'))
        assert remote.job_content_push_echo == '# Push result file content to output RIA storage:'
        assert remote.job_content_push_command == 'datalad push --to output-storage'
        assert remote.merge_content_remote == 'output-storage'

    def test_bare_git_content_push_targets_the_same_remote_as_the_ref_push(self, tmp_path):
        remote = BareGitOutputRemote(str(tmp_path / 'out.git'))
        command = remote.job_content_push_command
        # `outputstore` is the git remote participant_job.sh adds for
        # `${pushgitremote}`: one endpoint, both channels.
        assert 'outputstore' in command
        assert remote.merge_content_remote == 'origin'

    def test_bare_git_content_push_moves_no_refs(self, tmp_path):
        """Phase one must not publish the result branch: that is the marker."""
        command = BareGitOutputRemote(str(tmp_path / 'out.git')).job_content_push_command
        assert command.startswith('git annex copy')
        assert 'git push' not in command

    def test_ria_clone_source_keeps_the_dataset_id_fragment(self, tmp_path):
        remote = RiaOutputRemote(str(tmp_path / 'output_ria'))
        assert remote.clone_source('/ignored', 'abc-123') == (
            'ria+file://' + str(tmp_path / 'output_ria') + '#abc-123'
        )

    def test_bare_git_clone_source_is_the_endpoint(self, tmp_path):
        remote = BareGitOutputRemote(str(tmp_path / 'out.git'))
        assert remote.clone_source(str(tmp_path / 'out.git'), 'abc-123') == str(
            tmp_path / 'out.git'
        )


class TestGitEndpointResolution:
    def test_bare_git_endpoint_is_used_verbatim(self, tmp_path):
        """Regression for the `urlparse(...).path` truncation.

        The recorded push URL is handed to every compute job as
        `pushgitremote`; dropping `user@host` from it would make jobs push
        into a same-named local path instead of the intended host.
        """
        remote = BareGitOutputRemote(str(tmp_path / 'out.git'))
        recorded = 'ssh://user@host/srv/babs/out.git'
        assert remote.resolve_git_endpoint(recorded) == recorded

    def test_ria_endpoint_is_used_verbatim_when_it_is_a_dataset_dir(self, tmp_path):
        store = tmp_path / 'output_ria'
        data_dir = store / '238' / 'da2f2-uuid'
        (data_dir / '.git').mkdir(parents=True)
        remote = RiaOutputRemote(str(store))
        assert remote.resolve_git_endpoint(str(data_dir)) == str(data_dir)

    def test_ria_endpoint_falls_back_to_the_alias_symlink(self, tmp_path):
        store = tmp_path / 'output_ria'
        data_dir = store / '238' / 'da2f2-uuid'
        (data_dir / '.git').mkdir(parents=True)
        (store / 'alias').mkdir()
        (store / 'alias' / 'data').symlink_to(data_dir)
        remote = RiaOutputRemote(str(store))
        assert remote.resolve_git_endpoint(str(store)) == str(data_dir)

    def test_ria_ssh_push_url_is_not_truncated(self, tmp_path):
        """`ssh://user@host/path` must keep `user@host`."""
        remote = RiaOutputRemote(str(tmp_path / 'output_ria'))
        recorded = 'ssh://user@host/srv/babs-ria/238/da2f2-uuid'
        assert remote.resolve_git_endpoint(recorded) == recorded


class TestAnnexContentActuallyArrives:
    """The end of the content channel, exercised with real git-annex."""

    def _make_source(self, tmp_path):
        src = tmp_path / 'src'
        _git('init', '--initial-branch=main', str(src))
        _git('config', 'user.email', 'babs@example.com', cwd=src)
        _git('config', 'user.name', 'babs', cwd=src)
        subprocess.run(['git', 'annex', 'init', 'src'], cwd=src, capture_output=True, check=True)
        (src / 'sub-01_mriqc-24-0-2.zip').write_bytes(b'PK\x05\x06' + b'\0' * 18)
        subprocess.run(
            ['git', 'annex', 'add', 'sub-01_mriqc-24-0-2.zip'],
            cwd=src,
            capture_output=True,
            check=True,
        )
        _git('commit', '-m', 'result', cwd=src)
        return src

    def test_content_lands_in_an_annex_initialized_bare_repo(self, tmp_path):
        bare = tmp_path / 'out.git'
        remote = BareGitOutputRemote(str(bare))
        remote._ensure_bare_repo()
        remote._ensure_annex()

        src = self._make_source(tmp_path)
        _git('remote', 'add', 'outputstore', str(bare), cwd=src)
        subprocess.run(
            remote.job_content_push_command,
            cwd=src,
            shell=True,
            capture_output=True,
            check=True,
        )
        objects = list((bare / 'annex' / 'objects').rglob('*.zip'))
        assert objects, 'annexed content did not reach the bare repository'

    def test_a_bare_repo_without_an_annex_silently_swallows_content(self, tmp_path):
        """Why `_ensure_annex` exists: this is the failure it prevents."""
        bare = tmp_path / 'plain.git'
        _git('init', '--bare', str(bare))
        src = self._make_source(tmp_path)
        _git('remote', 'add', 'outputstore', str(bare), cwd=src)
        proc = subprocess.run(
            'git annex copy --to outputstore --in here .',
            cwd=src,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        # Either git-annex refuses, or it "succeeds" having moved nothing.
        # Both leave the endpoint without the result content.
        assert not list((bare / 'annex').rglob('*.zip')) if (bare / 'annex').exists() else True
        assert proc.returncode != 0 or not (bare / 'annex').exists()

    def test_ref_push_and_content_push_use_the_same_endpoint(self, tmp_path):
        """One URL carries both channels, so there is nothing extra to address."""
        bare = tmp_path / 'out.git'
        remote = BareGitOutputRemote(str(bare))
        remote._ensure_bare_repo()
        remote._ensure_annex()

        src = self._make_source(tmp_path)
        _git('checkout', '-b', 'job-1-1-sub-01', cwd=src)
        _git('remote', 'add', 'outputstore', str(bare), cwd=src)
        subprocess.run(
            remote.job_content_push_command, cwd=src, shell=True, capture_output=True, check=True
        )
        # Content is there, but no result branch yet: the job is not "done".
        assert list((bare / 'annex' / 'objects').rglob('*.zip'))
        assert list_result_branches(str(bare)) == {}

        _git('push', 'outputstore', 'job-1-1-sub-01', cwd=src)
        assert set(list_result_branches(str(bare))) == {'job-1-1-sub-01'}


class TestProvisioningGuards:
    """Each guard here corresponds to a way the store can be silently wrong."""

    def test_existing_non_empty_non_repo_is_refused(self, tmp_path):
        """A typo in --output-remote must not scatter git internals into data."""
        target = tmp_path / 'my-data'
        target.mkdir()
        (target / 'paper.txt').write_text('important')
        with pytest.raises(ValueError, match='not empty'):
            BareGitOutputRemote(str(target))._ensure_bare_repo()
        assert sorted(os.listdir(target)) == ['paper.txt']

    def test_existing_empty_directory_is_initialised(self, tmp_path):
        target = tmp_path / 'out.git'
        target.mkdir()
        remote = BareGitOutputRemote(str(target))
        remote._ensure_bare_repo()
        remote._ensure_annex()
        assert remote.annex_uuid()

    @pytest.mark.parametrize(
        'url',
        ['git@host:out.git', 'user@host:/srv/out.git', 'ssh://host/out.git', 'out.git'],
    )
    def test_non_local_or_relative_urls_are_refused(self, url):
        """scp-style URLs have no scheme, so a naive check treats them as paths
        and would `git init --bare` a local directory named `git@host:out.git`
        while every job pushes over ssh to a host that was never annex-inited."""
        with pytest.raises(ValueError, match='local path'):
            BareGitOutputRemote(url)


class TestSharedGroupAndHead:
    """Two things `babs init` must get right on the receiving repository."""

    def test_shared_group_applies_the_group_name(self, tmp_path, monkeypatch):
        """Regression: the group *name* was dropped.

        bootstrap always passes {'shared': 'group', 'group': <name>}, so a
        guard of `shared != 'group'` was never true and chgrp never ran. The
        repo got g+rws but the creating user's primary group, and the setgid
        bit propagated that wrong group to everything git-annex later wrote
        under annex/objects/ -- so a second member's job could not push.
        """
        seen = []
        real = subprocess.run

        def spy(argv, *a, **k):
            if argv and argv[0] == 'chgrp':
                seen.append(argv)
                return subprocess.CompletedProcess(argv, 0, '', '')
            return real(argv, *a, **k)

        monkeypatch.setattr(subprocess, 'run', spy)
        remote = BareGitOutputRemote(str(tmp_path / 'out.git'))
        remote._ensure_bare_repo('group', 'mylab')
        assert seen == [['chgrp', '-R', 'mylab', str(tmp_path / 'out.git')]]

    def test_finalize_points_head_at_the_published_branch(self, tmp_path):
        """A pre-existing bare repo's HEAD may name a branch BABS never pushes.

        `git push` does not re-point a bare repo's HEAD, so `ls-remote <url>
        HEAD` returns nothing *with exit 0*: check-setup then calls a fully
        populated endpoint empty, and `git remote show` reports `HEAD branch:
        (unknown)`, which stops `babs merge`.
        """
        bare = tmp_path / 'out.git'
        # created under `master`, as an older or differently-configured host would
        _git('init', '--bare', '-q', '-b', 'master', str(bare))
        work = tmp_path / 'work'
        _git('init', '-q', '-b', 'main', str(work))
        for k, v in (('user.email', 't@e.st'), ('user.name', 'T')):
            _git('config', k, v, cwd=work)
        (work / 'f.txt').write_text('x')
        _git('add', 'f.txt', cwd=work)
        _git('commit', '-qm', 'init', cwd=work)
        _git('push', '-q', str(bare), 'main', cwd=work)

        assert _git('ls-remote', str(bare), 'HEAD') == ''  # dangling

        BareGitOutputRemote(str(bare)).finalize(str(bare))

        assert _git('ls-remote', str(bare), 'HEAD').split()[0] == _git(
            'rev-parse', 'main', cwd=work
        )

    def test_finalize_leaves_a_resolving_head_alone(self, tmp_path):
        bare = tmp_path / 'out.git'
        _git('init', '--bare', '-q', '-b', 'main', str(bare))
        work = tmp_path / 'work'
        _git('init', '-q', '-b', 'main', str(work))
        for k, v in (('user.email', 't@e.st'), ('user.name', 'T')):
            _git('config', k, v, cwd=work)
        (work / 'f.txt').write_text('x')
        _git('add', 'f.txt', cwd=work)
        _git('commit', '-qm', 'init', cwd=work)
        _git('push', '-q', str(bare), 'main', cwd=work)
        _git('push', '-q', str(bare), 'main:other', cwd=work)

        BareGitOutputRemote(str(bare)).finalize(str(bare))
        assert _git('-C', str(bare), 'symbolic-ref', 'HEAD') == 'refs/heads/main'


class TestRejectedUrlsExplainTheRule:
    """Each rejection should name the rule it broke, not a generic message."""

    @pytest.mark.parametrize(
        ('url', 'because'),
        [
            ('/srv/a b/out.git', 'whitespace'),
            ('out.git', 'relative path'),
            ('ssh://host/out.git', 'is a URL'),
            ('git@host:out.git', 'scp-style'),
            ('', 'empty'),
        ],
    )
    def test_message_names_the_rule(self, url, because):
        with pytest.raises(ValueError, match=because):
            BareGitOutputRemote(url)

    def test_whitespace_is_refused_before_it_can_break_every_job(self):
        """The submit template embeds the push URL unquoted and the scheduler
        splits the command on whitespace, so a path with a space becomes two
        argv entries: the job reads its subject list from the tail of the path
        and aborts. `babs init` and `check-setup` both pass first."""
        with pytest.raises(ValueError, match='whitespace'):
            BareGitOutputRemote('/srv/with space/out.git')

    def test_tilde_is_expanded_not_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv('HOME', str(tmp_path))
        assert BareGitOutputRemote('~/out.git').url == str(tmp_path / 'out.git')

    def test_an_existing_plain_file_is_refused_clearly(self, tmp_path):
        """Regression: os.listdir on a file raised NotADirectoryError, so the
        user got a traceback instead of the validated message."""
        target = tmp_path / 'notes.txt'
        target.write_text('data')
        with pytest.raises(ValueError, match='is a file, not a directory'):
            BareGitOutputRemote(str(target))._ensure_bare_repo()


class TestAnnexIgnoreIsReadNotForced:
    """`annex-ignore=true` means git-annex found no annex; that is often right.

    The RIA default is the standing example: its *git* sibling carries no
    content, so git-annex marks it ignored and content goes to the ORA remote.
    Clearing the flag blindly turns a condition git-annex detected correctly
    into a silent content-loss, so it is only ever cleared against evidence.
    """

    @staticmethod
    def _analysis(tmp_path):
        analysis = tmp_path / 'analysis'
        analysis.mkdir()
        _git('init', '-q', cwd=analysis)
        return analysis

    @staticmethod
    def _annexed_remote(tmp_path):
        remote = BareGitOutputRemote(str(tmp_path / 'out.git'))
        remote._ensure_bare_repo()
        remote._ensure_annex()
        return remote

    def _ignore_value(self, analysis):
        return _git('config', '--get', 'remote.output.annex-ignore', cwd=analysis, check=False)

    def test_unset_is_left_alone(self, tmp_path):
        analysis = self._analysis(tmp_path)
        self._annexed_remote(tmp_path)._clear_stale_annex_ignore(str(analysis))
        assert self._ignore_value(analysis) == ''

    def test_false_is_left_alone(self, tmp_path):
        analysis = self._analysis(tmp_path)
        _git('config', 'remote.output.annex-ignore', 'false', cwd=analysis)
        self._annexed_remote(tmp_path)._clear_stale_annex_ignore(str(analysis))
        assert self._ignore_value(analysis) == 'false'

    def test_stale_true_on_a_real_annex_is_cleared_with_a_warning(self, tmp_path):
        analysis = self._analysis(tmp_path)
        _git('config', 'remote.output.annex-ignore', 'true', cwd=analysis)
        remote = self._annexed_remote(tmp_path)
        with pytest.warns(UserWarning, match='annex-ignore'):
            remote._clear_stale_annex_ignore(str(analysis))
        assert self._ignore_value(analysis) == 'false'

    def test_true_without_an_annex_raises_instead_of_being_overridden(self, tmp_path):
        """The case that must not be papered over: no annex means no content."""
        analysis = self._analysis(tmp_path)
        _git('config', 'remote.output.annex-ignore', 'true', cwd=analysis)
        plain = BareGitOutputRemote(str(tmp_path / 'plain.git'))
        plain._ensure_bare_repo()  # deliberately *not* annex-inited
        with pytest.raises(ValueError, match='annex-ignore'):
            plain._clear_stale_annex_ignore(str(analysis))
        # and the flag git-annex set is left as it was
        assert self._ignore_value(analysis) == 'true'


class TestContentRemoteNaming:
    """The content sibling is named differently in each clone; check-setup and
    merge each need the name for *their* context, not a single constant."""

    def test_ria_uses_the_ora_name_everywhere(self, tmp_path):
        remote = RiaOutputRemote(str(tmp_path / 'output_ria'))
        assert remote.analysis_content_remote == 'output-storage'
        assert remote.merge_content_remote == 'output-storage'

    def test_bare_git_carries_content_on_the_ref_sibling(self, tmp_path):
        remote = BareGitOutputRemote(str(tmp_path / 'out.git'))
        # in `analysis` the single sibling is `output`; `merge_ds` is a clone
        # of the repository itself, so there it is `origin`
        assert remote.analysis_content_remote == 'output'
        assert remote.merge_content_remote == 'origin'
