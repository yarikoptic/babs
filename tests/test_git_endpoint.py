"""Unit tests for the URL-addressed git helpers in `babs.git_endpoint`.

These run against real `git init --bare` fixtures in `tmp_path`; no datalad,
no cluster, no network.
"""

import subprocess

import pytest

from babs.git_endpoint import (
    GitEndpointError,
    delete_result_branches,
    endpoint_exists,
    endpoint_head_hash,
    list_result_branches,
    ls_remote_heads,
)


def _git(*args, cwd=None):
    return subprocess.run(
        ['git', *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def bare_endpoint(tmp_path):
    """A bare repo with a default branch and two `job-*` result branches."""
    bare = tmp_path / 'output.git'
    _git('init', '--bare', '--initial-branch=main', str(bare))

    work = tmp_path / 'work'
    _git('init', '--initial-branch=main', str(work))
    _git('config', 'user.email', 'babs@example.com', cwd=work)
    _git('config', 'user.name', 'babs', cwd=work)
    (work / 'a.txt').write_text('base\n')
    _git('add', 'a.txt', cwd=work)
    _git('commit', '-m', 'base', cwd=work)
    _git('push', str(bare), 'main:refs/heads/main', cwd=work)

    oids = {}
    for name in ('job-1-1-sub-01', 'job-1-2-sub-02'):
        _git('checkout', '-b', name, 'main', cwd=work)
        (work / f'{name}.txt').write_text('result\n')
        _git('add', f'{name}.txt', cwd=work)
        _git('commit', '-m', name, cwd=work)
        oids[name] = _git('rev-parse', 'HEAD', cwd=work)
        _git('push', str(bare), f'{name}:refs/heads/{name}', cwd=work)
        _git('checkout', 'main', cwd=work)

    return {'bare': bare, 'work': work, 'oids': oids}


class TestListing:
    def test_ls_remote_heads_lists_every_branch(self, bare_endpoint):
        heads = ls_remote_heads(str(bare_endpoint['bare']))
        assert set(heads) == {'main', 'job-1-1-sub-01', 'job-1-2-sub-02'}
        assert heads['job-1-1-sub-01'] == bare_endpoint['oids']['job-1-1-sub-01']

    def test_list_result_branches_filters_and_carries_oids(self, bare_endpoint):
        branches = list_result_branches(str(bare_endpoint['bare']))
        assert branches == bare_endpoint['oids']

    def test_empty_endpoint_returns_empty_mapping(self, tmp_path):
        empty = tmp_path / 'empty.git'
        _git('init', '--bare', str(empty))
        assert list_result_branches(str(empty)) == {}

    def test_unreachable_endpoint_raises_rather_than_reporting_no_results(self, tmp_path):
        """A transport failure must never look like 'this job produced nothing'."""
        with pytest.raises(GitEndpointError):
            list_result_branches(str(tmp_path / 'does-not-exist.git'))

    def test_error_message_names_the_endpoint(self, tmp_path):
        missing = tmp_path / 'nope.git'
        with pytest.raises(GitEndpointError, match=str(missing)):
            list_result_branches(str(missing))

    def test_utils_helper_no_longer_swallows_transport_errors(self, tmp_path, bare_endpoint):
        """`get_results_branches_from_ria` used to `return []` on failure."""
        from babs.utils import get_results_branches_from_ria

        assert get_results_branches_from_ria(str(bare_endpoint['bare'])) == sorted(
            bare_endpoint['oids']
        )
        with pytest.raises(GitEndpointError):
            get_results_branches_from_ria(str(tmp_path / 'nope.git'))


class TestHead:
    def test_head_hash_matches_default_branch(self, bare_endpoint):
        head = endpoint_head_hash(str(bare_endpoint['bare']))
        assert head == _git('rev-parse', 'main', cwd=bare_endpoint['work'])

    def test_head_hash_raises_for_unreachable(self, tmp_path):
        with pytest.raises(GitEndpointError):
            endpoint_head_hash(str(tmp_path / 'gone.git'))

    def test_head_hash_raises_for_empty_repo(self, tmp_path):
        empty = tmp_path / 'empty.git'
        _git('init', '--bare', str(empty))
        with pytest.raises(GitEndpointError, match='no HEAD'):
            endpoint_head_hash(str(empty))

    def test_endpoint_exists(self, bare_endpoint, tmp_path):
        assert endpoint_exists(str(bare_endpoint['bare'])) is True
        assert endpoint_exists(str(tmp_path / 'gone.git')) is False


class TestLeaseSafeDeletion:
    def test_deletes_branches_at_the_expected_oid(self, bare_endpoint):
        url = str(bare_endpoint['bare'])
        delete_result_branches(url, bare_endpoint['oids'])
        assert list_result_branches(url) == {}

    def test_refuses_to_delete_a_branch_that_moved(self, bare_endpoint):
        url = str(bare_endpoint['bare'])
        work = bare_endpoint['work']
        moved = 'job-1-1-sub-01'
        # Another job re-pushes the branch after we read it.
        _git('checkout', moved, cwd=work)
        (work / 'later.txt').write_text('later\n')
        _git('add', 'later.txt', cwd=work)
        _git('commit', '-m', 'later', cwd=work)
        _git(
            'push', '--force', str(bare_endpoint['bare']), f'{moved}:refs/heads/{moved}', cwd=work
        )

        with pytest.raises(GitEndpointError):
            delete_result_branches(url, {moved: bare_endpoint['oids'][moved]})
        assert moved in list_result_branches(url)

    def test_empty_request_is_a_noop(self, bare_endpoint):
        assert delete_result_branches(str(bare_endpoint['bare']), {}) == ''

    def test_unreachable_endpoint_raises(self, tmp_path):
        with pytest.raises(GitEndpointError):
            delete_result_branches(str(tmp_path / 'gone.git'), {'job-1': 'a' * 40})
