"""What kind of thing a BABS remote address names.

Deciding whether a string is a filesystem path, a URL, or git's scp-style ssh
syntax is not BABS's problem to solve: datalad already answers it, for exactly
the addresses BABS deals in.  ``datalad.support.network.RI`` returns a
``PathRI``, a ``URL`` or an ``SSHRI``, and ``.localpath`` yields the filesystem
path when there is one.  In particular ``git@host:out.git`` comes back as an
``SSHRI`` -- the case a naive check gets wrong, since it carries no scheme for
``urlparse`` to report, and mistaking it for a path would have BABS create a
local directory of that name while every job pushed over ssh to a host that
was never annex-inited.

This module is the single place BABS imports that from, and it holds the few
checks that are BABS *policy* rather than URL semantics: an address must not
be empty, must not contain whitespace (the generated submit template embeds it
unquoted, and the scheduler splits the command on whitespace), and must be
absolute (git resolves a relative remote against ``analysis/``, not the
directory ``babs init`` ran in).
"""

import os.path as op
import subprocess

from datalad.support.network import RI, SSHRI

#: Scheme prefix datalad gives a RIA store: ``ria+file``, ``ria+ssh``, ...
RIA_PREFIX = 'ria+'


def parse(value):
    """The `RI` for `value`, or ``None`` if datalad cannot parse it at all."""
    if not value:
        return None
    try:
        return RI(value)
    except ValueError:
        return None


def local_path(value):
    """The filesystem path `value` names, or ``None`` when it names none.

    Understands plain paths, ``file://`` URLs and ``ria+file://`` stores. A
    RIA store's ``#<dataset-id>`` is a URL fragment addressing a dataset
    *inside* the store, so it is not part of the path -- while a ``#`` in a
    plain path is just a character in a directory name, which is why this is
    asked of `RI` rather than done with a string split.
    """
    ri = parse(value)
    if ri is None:
        return None
    scheme = getattr(ri, 'scheme', None) or ''
    if scheme.startswith(RIA_PREFIX):
        # Only a `ria+file` store is on this filesystem.
        return (ri.path or None) if scheme == RIA_PREFIX + 'file' else None
    try:
        return ri.localpath or None
    except ValueError:
        return None


def usable_local_path(value):
    """The absolute path `value` names, or ``None`` if BABS cannot use it.

    ``~`` is expanded; see :func:`why_not_usable` for what is refused.
    """
    path = local_path(value)
    if path is None or any(c.isspace() for c in path):
        return None
    path = op.expanduser(path)
    return op.abspath(path) if op.isabs(path) else None


def why_not_usable(value):
    """One sentence naming the rule `value` broke, for an error message."""
    if not value:
        return 'it is empty.'
    ri = parse(value)
    if isinstance(ri, SSHRI):
        return "it is an ssh URL in git's scp-style syntax, not a filesystem path."
    path = local_path(value)
    if path is None:
        return 'it is a URL, not a filesystem path.'
    if any(c.isspace() for c in path):
        return (
            'it contains whitespace, which the generated job submission command '
            'would split into separate arguments.'
        )
    if not op.isabs(op.expanduser(path)):
        return (
            'it is a relative path; git would resolve it against `analysis/` rather '
            'than the directory `babs init` ran in.'
        )
    return 'it is not a usable local path.'


def is_ria(value):
    """Whether `value` addresses a RIA store (``ria+file://``, ``ria+ssh://``, ...)."""
    ri = parse(value)
    return bool(getattr(ri, 'scheme', '') or '') and ri.scheme.startswith(RIA_PREFIX)


def is_file_url(value):
    """Whether `value` was written as a ``file://`` URL rather than a bare path.

    The distinction is what the user *asked for* when the target does not
    exist yet: a bare path means a RIA store (BABS's historical default),
    while ``file://`` names a plain git repository.
    """
    ri = parse(value)
    return getattr(ri, 'scheme', None) == 'file'


def existing_kind(path):
    """What is already at `path`: ``'ria'``, ``'bare'``, ``'worktree'`` or None.

    Asked of the target itself rather than inferred from its name, so that a
    store whose directory does not follow any naming convention is still
    recognised for what it is.
    """
    if not path or not op.isdir(path):
        return None
    if op.exists(op.join(path, 'ria-layout-version')):
        return None if op.exists(op.join(path, '.git')) else 'ria'
    proc = subprocess.run(
        ['git', 'rev-parse', '--is-bare-repository'],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return 'bare' if proc.stdout.strip() == 'true' else 'worktree'
