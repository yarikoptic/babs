"""Git plumbing that addresses a repository by URL instead of by local path.

Historically BABS reached into the output RIA store as a *local directory*:
``git branch --list`` with ``cwd=<store>``, ``git branch --delete`` with
``cwd=<store>``, ``git rev-parse HEAD`` with ``cwd=<store>``.  None of those
can be pointed at a URL, which is the hardest local-only coupling in the
status/merge path.

The helpers here are the git-protocol equivalents.  They take anything
``git`` accepts as a repository location (a path, ``ssh://``, ``https://``,
...) and never change directory.

Error handling is deliberate: an unreachable endpoint **raises**.  Returning
an empty list on a transport failure would make ``babs status`` report "no
job has finished" and ``babs merge`` refuse to merge results that exist.
"""

import subprocess

#: Job result branches are published under this prefix.
RESULTS_BRANCH_PREFIX = 'job-'

#: Seconds before a git endpoint operation is considered hung.
DEFAULT_TIMEOUT = 60

_HEADS_PREFIX = 'refs/heads/'


class GitEndpointError(RuntimeError):
    """A git endpoint could not be reached, or refused an operation.

    This is intentionally *not* silently swallowed anywhere: a transport
    failure must never be mistaken for "there are no results".
    """


def _run_git(args, what, timeout=DEFAULT_TIMEOUT):
    """Run a git command, raising :class:`GitEndpointError` on any failure."""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitEndpointError(
            f'{what} timed out after {timeout} seconds. Command: {" ".join(args)}'
        ) from exc
    except OSError as exc:  # e.g. git is not installed
        raise GitEndpointError(f'{what} could not be run: {exc}') from exc
    if proc.returncode != 0:
        raise GitEndpointError(
            f'{what} failed with exit code {proc.returncode}.\n'
            f'Command: {" ".join(args)}\n'
            f'stderr: {proc.stderr.strip()}'
        )
    return proc.stdout


def ls_remote_heads(url, timeout=DEFAULT_TIMEOUT):
    """List all branches of a git endpoint.

    Parameters
    ----------
    url : str
        Anything ``git ls-remote`` accepts: a filesystem path or a URL.
    timeout : int, optional
        Seconds to wait before giving up.

    Returns
    -------
    dict
        Mapping of branch name to commit hash (object id).

    Raises
    ------
    GitEndpointError
        If the endpoint cannot be reached or git fails for any reason.
    """
    stdout = _run_git(
        ['git', 'ls-remote', '--heads', str(url)],
        f"listing branches of '{url}'",
        timeout=timeout,
    )
    heads = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        oid, ref = parts[0], parts[1]
        if ref.startswith(_HEADS_PREFIX):
            heads[ref[len(_HEADS_PREFIX) :]] = oid
    return heads


def list_result_branches(url, timeout=DEFAULT_TIMEOUT):
    """Return the ``job-*`` result branches of a git endpoint as ``{name: oid}``.

    Raises
    ------
    GitEndpointError
        If the endpoint cannot be reached.  An empty dict means "reached the
        endpoint, and it holds no result branches" -- never "could not ask".
    """
    return {
        name: oid
        for name, oid in ls_remote_heads(url, timeout=timeout).items()
        if name.startswith(RESULTS_BRANCH_PREFIX)
    }


def endpoint_head_hash(url, timeout=DEFAULT_TIMEOUT):
    """Return the commit hash that ``HEAD`` resolves to at a git endpoint.

    This is the endpoint equivalent of ``git rev-parse HEAD`` with ``cwd=``.

    Raises
    ------
    GitEndpointError
        If the endpoint cannot be reached, or has no ``HEAD``.
    """
    stdout = _run_git(
        ['git', 'ls-remote', str(url), 'HEAD'],
        f"reading HEAD of '{url}'",
        timeout=timeout,
    )
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == 'HEAD':
            return parts[0]
    raise GitEndpointError(f"The git endpoint '{url}' has no HEAD (is it empty?).")


def endpoint_exists(url, timeout=DEFAULT_TIMEOUT):
    """Return whether a git endpoint is reachable and looks like a repository."""
    try:
        _run_git(
            ['git', 'ls-remote', str(url), 'HEAD'],
            f"probing '{url}'",
            timeout=timeout,
        )
    except GitEndpointError:
        return False
    return True


def delete_result_branches(url, branch_oids, timeout=DEFAULT_TIMEOUT):
    """Delete branches at a git endpoint, guarded by an expected-OID lease.

    Each branch is only deleted if the endpoint still has it at exactly the
    recorded object id, so a job that pushed *after* the merge read the
    branch is never silently discarded.

    Note the semantic difference from ``git branch --delete``, which refuses
    to delete a branch that is not merged into HEAD.  A lease only checks the
    object id; the "is it merged" guarantee comes from ``babs merge`` pushing
    the merge commit before it deletes anything.

    Parameters
    ----------
    url : str
        The git endpoint holding the branches.
    branch_oids : dict
        Mapping of branch name to the object id it is expected to be at.
    timeout : int, optional
        Seconds to wait before giving up.

    Returns
    -------
    str
        git's output, for logging.

    Raises
    ------
    GitEndpointError
        If the endpoint is unreachable or any lease is stale.
    """
    if not branch_oids:
        return ''
    # --atomic: one refused lease must not leave the other branches in the
    # chunk deleted. The caller only sees an exception and cannot tell which
    # refs went through, so a partial delete would silently drop results.
    args = ['git', 'push', '--atomic', '--delete']
    for name, oid in branch_oids.items():
        args.append(f'--force-with-lease={_HEADS_PREFIX}{name}:{oid}')
    args.append(str(url))
    args.extend(f'{_HEADS_PREFIX}{name}' for name in branch_oids)
    return _run_git(args, f"deleting merged branches at '{url}'", timeout=timeout)
