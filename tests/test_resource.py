"""Classification of remote addresses, which is datalad's `RI`, plus policy."""

import pytest

from babs import resource


class TestLocalPath:
    @pytest.mark.parametrize(
        ('value', 'expected'),
        [
            ('/srv/out.git', '/srv/out.git'),
            ('out.git', 'out.git'),  # relative: a path, though not a usable one
            ('file:///srv/out.git', '/srv/out.git'),
            ('ria+file:///srv/store', '/srv/store'),
            # the RIA dataset id is a URL fragment, not part of the store path
            ('ria+file:///srv/store#123-abc', '/srv/store'),
            # not on this filesystem
            ('ria+ssh://host/srv/store', None),
            ('ssh://host/srv/out.git', None),
            ('https://forge.org/u/r.git', None),
            ('git@host:out.git', None),
            ('user@host:/srv/out.git', None),
            ('host:/srv/out.git', None),
            ('', None),
        ],
    )
    def test_classification(self, value, expected):
        assert resource.local_path(value) == expected

    def test_a_hash_in_a_plain_path_is_not_a_fragment(self):
        """Splitting on `#` unconditionally truncated a legitimate directory
        name; `RI` treats it as a fragment only where it is one."""
        assert resource.local_path('/srv/study#2/out.git') == '/srv/study#2/out.git'


class TestUsableLocalPath:
    def test_absolute_paths_are_returned_absolute(self):
        assert resource.usable_local_path('/srv/out.git') == '/srv/out.git'

    def test_tilde_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv('HOME', str(tmp_path))
        assert resource.usable_local_path('~/out.git') == str(tmp_path / 'out.git')

    @pytest.mark.parametrize(
        'value',
        ['out.git', './out.git', '/srv/a b/out.git', 'ssh://host/x', 'git@host:x', ''],
    )
    def test_refused(self, value):
        assert resource.usable_local_path(value) is None


class TestWhyNotUsable:
    @pytest.mark.parametrize(
        ('value', 'because'),
        [
            ('', 'empty'),
            ('git@host:out.git', 'scp-style'),
            ('host:/srv/out.git', 'scp-style'),
            ('ssh://host/out.git', 'is a URL'),
            ('ria+ssh://host/store', 'is a URL'),
            ('/srv/a b/out.git', 'whitespace'),
            ('out.git', 'relative path'),
        ],
    )
    def test_names_the_rule_that_was_broken(self, value, because):
        assert because in resource.why_not_usable(value)
