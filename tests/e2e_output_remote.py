#!/usr/bin/env python
"""End-to-end exercise of BABS result publication, for each output remote.

This drives the *real* thing, with no cluster:

    babs init -> babs check-setup -> the generated `participant_job.sh`
    (run directly, for two subjects, with the arguments taken from the
    generated `submit_job_template.yaml`) -> babs status -> babs merge
    -> a fresh clone that retrieves the merged results.

It runs once per provider:

* ``ria``      -- the historical default, unchanged: an output RIA store
                  inside the project root, with an ORA content sibling.
* ``bare-git`` -- ``babs init --output-remote file:///path/to/output.git``: one
                  plain bare git repository carrying both publication channels.
* ``worktree-git`` -- ``babs init --output-remote file:///path/to/output``: a
                  regular repository with a worktree, receiving pushes through
                  ``receive.denyCurrentBranch=updateInstead``.
* ``remote-git`` -- ``babs init --output-remote ssh://host/path/to/output.git``:
                  an endpoint reached over a git transport, which BABS validates
                  rather than creates. Requires an ssh host that can reach the
                  path; pass ``--ssh-host`` (see ``--help``), which is expected
                  to be a `Host` alias in the caller's ``~/.ssh/config``.

The point of running both is that the second must work *and* the first must
still work.

The BIDS input is the ds000003-demo layout, and the BIDS App is mriqc-shaped
(one output folder, ``all_results_in_one_zip``, ReproNim/containers-style
image registration). There is no container runtime and no network here, so
``singularity`` and ``7z`` are shimmed with small wrappers placed on ``PATH``
via the project's own ``script_preamble`` -- the generated scripts themselves
are run unmodified -- and the ``.nii.gz`` files are placeholder blobs.

Usage::

    python tests/e2e_output_remote.py                 # both providers
    python tests/e2e_output_remote.py --provider ria
    python tests/e2e_output_remote.py --workdir /some/scratch --keep
"""

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CONTAINER_NAME = 'mriqc-24-0-2'
ZIP_FOLDERNAME = 'mriqc'
ZIP_VERSION = '24-0-2'
SUBJECTS = ['sub-01', 'sub-02']
TASK = 'rhymejudgment'

# ReproNim/containers registers images as images/<collection>/<app>--<version>.sing
IMAGE_RELPATH = 'images/bids/bids-mriqc--24.0.2.sing'


REPO_ROOT = Path(__file__).resolve().parent.parent

# `babs` may be installed (editable) from a *different* checkout, so make sure
# both this script and every subprocess pick up *this* tree, and never shell
# out to the installed `babs` console script.
sys.path.insert(0, str(REPO_ROOT))
os.environ['PYTHONPATH'] = os.pathsep.join(
    [str(REPO_ROOT), *([os.environ['PYTHONPATH']] if os.environ.get('PYTHONPATH') else [])]
)
BABS_CLI = [sys.executable, '-c', 'import sys; from babs.cli import _main; sys.exit(_main())']


def assert_using_this_tree():
    """Fail early if `import babs` resolves somewhere other than this tree."""
    import babs

    resolved = Path(babs.__file__).resolve()
    if REPO_ROOT not in resolved.parents:
        raise E2EFailure(
            f'`import babs` resolved to {resolved}, not to this tree ({REPO_ROOT}). '
            'The end-to-end test would be exercising a different checkout.'
        )
    print(f'Testing babs from {resolved.parent}')


class E2EFailure(RuntimeError):
    """An end-to-end step did not do what it is supposed to do."""


def run(cmd, cwd=None, env=None, capture=True, check=True):
    """Run a command, echoing it, and fail loudly with its output."""
    printable = cmd if isinstance(cmd, str) else ' '.join(str(c) for c in cmd)
    if cwd is None and not isinstance(cmd, str) and cmd[:3] == BABS_CLI:
        cwd = REPO_ROOT
    print(f'  $ {printable}', flush=True)
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        shell=isinstance(cmd, str),
        capture_output=capture,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise E2EFailure(
            f'Command failed (exit {proc.returncode}): {printable}\n'
            f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}'
        )
    return proc


# ---------------------------------------------------------------------------
# fixtures: shims, input data, container dataset, config
# ---------------------------------------------------------------------------
SINGULARITY_SHIM = r"""#!/bin/bash
# Stand-in for `singularity`: no container runtime is available here, so this
# produces mriqc-shaped derivatives instead of running the image.
set -eu
args=("$@")
input_dir=""
output_dir=""
label=""
for ((i = 0; i < ${#args[@]}; i++)); do
  if [ "${args[$i]}" = "participant" ] && [ "$i" -ge 2 ]; then
    input_dir="${args[$((i - 2))]}"
    output_dir="${args[$((i - 1))]}"
  fi
  if [ "${args[$i]}" = "--participant-label" ]; then
    label="${args[$((i + 1))]}"
  fi
done
if [ -z "$output_dir" ] || [ -z "$label" ]; then
  echo "singularity shim: could not parse args: $*" >&2
  exit 2
fi
if [ ! -d "$input_dir/$label" ]; then
  echo "singularity shim: input subject dir missing: $input_dir/$label" >&2
  exit 3
fi
mkdir -p "$output_dir/$label/anat" "$output_dir/$label/func" "$output_dir/logs"
printf '{"Name": "MRIQC - MRI Quality Control", "BIDSVersion": "1.0.0"}\n' \
  > "$output_dir/dataset_description.json"
printf '{"subject_id": "%s", "cjv": 0.42, "efc": 0.51}\n' "$label" \
  > "$output_dir/$label/anat/${label}_T1w.json"
printf '{"subject_id": "%s", "fd_mean": 0.13, "tsnr": 41.2}\n' "$label" \
  > "$output_dir/$label/func/${label}_task-TASKNAME_bold.json"
printf '<html><body>%s report</body></html>\n' "$label" > "$output_dir/${label}_T1w.html"
echo "singularity shim: wrote mriqc-shaped outputs for $label into $output_dir"
"""

SEVENZIP_SHIM = r"""#!/bin/bash
# Stand-in for `7z`, backed by zip/unzip: BABS's generated scripts call
# `7z a <archive> <dir>` to zip results and `7z x <archive>` to unzip inputs.
set -eu
mode="${1:-}"
shift || true
case "$mode" in
  a)
    archive="$1"
    shift
    exec zip -q -r "$archive" "$@"
    ;;
  x)
    archive="$1"
    shift
    exec unzip -q -o "$archive"
    ;;
  *)
    echo "7z shim: unsupported mode '$mode'" >&2
    exit 2
    ;;
esac
"""


def write_shims(bindir):
    """Write the `singularity` and `7z` stand-ins and return their directory."""
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / 'singularity').write_text(SINGULARITY_SHIM.replace('TASKNAME', TASK))
    (bindir / '7z').write_text(SEVENZIP_SHIM)
    for name in ('singularity', '7z'):
        (bindir / name).chmod(0o755)
    return bindir


def _placeholder_nifti(path):
    """A small gzip blob standing in for a .nii.gz we cannot download."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, 'wb') as f:
        f.write(b'placeholder NIfTI for ' + path.name.encode() + b'\n')


def make_bids_dataset(path):
    """Create a ds000003-demo shaped BIDS dataset as a datalad dataset."""
    print(f'\n== Creating input BIDS dataset at {path}')
    run(['datalad', 'create', str(path)])
    (path / 'dataset_description.json').write_text(
        json.dumps({'Name': 'ds000003-demo', 'BIDSVersion': '1.0.2'}, indent=2) + '\n'
    )
    (path / f'task-{TASK}_bold.json').write_text(
        json.dumps({'RepetitionTime': 2.0, 'TaskName': TASK}, indent=2) + '\n'
    )
    (path / 'participants.tsv').write_text(
        'participant_id\tage\tsex\n' + ''.join(f'{s}\t25\tM\n' for s in SUBJECTS)
    )
    for sub in SUBJECTS:
        _placeholder_nifti(path / sub / 'anat' / f'{sub}_T1w.nii.gz')
        _placeholder_nifti(path / sub / 'func' / f'{sub}_task-{TASK}_bold.nii.gz')
        events = path / sub / 'func' / f'{sub}_task-{TASK}_events.tsv'
        events.write_text('onset\tduration\ttrial_type\n0.0\t2.0\tword\n')
    run(['datalad', 'save', '-m', 'Add ds000003-demo data'], cwd=path)
    return path


def make_container_dataset(path):
    """Create a container datalad dataset in ReproNim/containers layout."""
    print(f'\n== Creating container dataset at {path}')
    run(['datalad', 'create', str(path)])
    image = path / IMAGE_RELPATH
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b'#!/not-a-real-singularity-image\n')
    config = path / '.datalad' / 'config'
    config.write_text(
        config.read_text()
        + f'[datalad "containers.{CONTAINER_NAME}"]\n'
        + f'\timage = {IMAGE_RELPATH}\n'
        + '\tcmdexec = singularity exec {img} {cmd}\n'
    )
    run(['datalad', 'save', '-m', 'Add mriqc image and its registration'], cwd=path)
    return path


def write_container_config(path, bids_path, shim_dir, scratch_dir):
    """Write an mriqc-shaped BABS container config."""
    text = f"""\
input_datasets:
    BIDS:
        required_files:
            - "anat/*_T1w.nii*"
        is_zipped: false
        origin_url: "{bids_path}"
        path_in_babs: inputs/data/BIDS

bids_app_args:
    $SUBJECT_SELECTION_FLAG: "--participant-label"
    --no-sub: ""
    -vv: ""

all_results_in_one_zip: true
zip_foldernames:
    {ZIP_FOLDERNAME}: "{ZIP_VERSION}"

singularity_args:
    - --containall

cluster_resources:
    interpreting_shell: "/bin/bash"

script_preamble: |
    export PATH="{shim_dir}:$PATH"

job_compute_space: "{scratch_dir}"
"""
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# driving the generated job script
# ---------------------------------------------------------------------------
def parse_submit_template(analysis_path):
    """Pull the real job invocation out of the generated submit template.

    Returns (env_assignments, argv) where argv starts with the generated
    `participant_job.sh`. Nothing here is reconstructed by hand: whatever
    `babs init` recorded is what gets run.
    """
    import yaml

    template = yaml.safe_load((analysis_path / 'code' / 'submit_job_template.yaml').read_text())
    tokens = template['cmd_template'].split()
    env = {}
    for token in tokens:
        if token.startswith('--export='):
            for assignment in token[len('--export=') :].split(','):
                key, _, value = assignment.partition('=')
                env[key] = value
    for index, token in enumerate(tokens):
        if token.endswith('participant_job.sh'):
            return env, tokens[index:]
    raise E2EFailure(f'No participant_job.sh in cmd_template: {template["cmd_template"]!r}')


def write_job_submit_csv(analysis_path, job_id=1):
    """Write `code/job_submit.csv` the way `babs submit` would."""
    csv_path = analysis_path / 'code' / 'job_submit.csv'
    lines = ['sub_id,job_id,task_id']
    lines += [f'{sub},{job_id},{i + 1}' for i, sub in enumerate(SUBJECTS)]
    csv_path.write_text('\n'.join(lines) + '\n')
    return csv_path


def run_participant_job(analysis_path, task_id, job_id=1):
    """Run the generated participant_job.sh for one array task."""
    export_env, argv = parse_submit_template(analysis_path)
    env = dict(os.environ)
    env.update(export_env)
    env['SLURM_ARRAY_JOB_ID'] = str(job_id)
    env['SLURM_ARRAY_TASK_ID'] = str(task_id)
    print(f'\n-- Running the generated participant_job.sh for task {task_id}')
    proc = run(['bash', *argv], env=env, check=False)
    if proc.returncode != 0 or 'SUCCESS' not in proc.stdout:
        raise E2EFailure(
            f'participant_job.sh (task {task_id}) failed with exit {proc.returncode}\n'
            f'--- stdout (tail) ---\n{proc.stdout[-6000:]}\n'
            f'--- stderr (tail) ---\n{proc.stderr[-6000:]}'
        )
    return proc


# ---------------------------------------------------------------------------
# assertions
# ---------------------------------------------------------------------------
def expect(condition, message):
    if not condition:
        raise E2EFailure(message)


def zip_name(sub):
    return f'{sub}_{ZIP_FOLDERNAME}-{ZIP_VERSION}.zip'


def check_result_branches(babs_proj, expected_count):
    branches = babs_proj._get_results_branches()
    expect(
        len(branches) == expected_count,
        f'Expected {expected_count} result branch(es) at {babs_proj.output_git_url}, '
        f'got {len(branches)}: {branches}',
    )
    return branches


def check_two_phase_publication(analysis_path, babs_proj):
    """Content must be published before the result branch, never after.

    The result branch is the completion marker: `babs status` and `babs merge`
    treat its presence as "this job finished". If the ref went first, a
    crash in between would advertise results whose content is nowhere.
    """
    script = (analysis_path / 'code' / 'participant_job.sh').read_text()
    content_cmd = f'git annex copy --to {babs_proj.output_remote.job_content_remote} --in here .'
    content_at = script.find(content_cmd)
    ref_at = script.find('git push outputstore')
    expect(content_at != -1, f'participant_job.sh does not run {content_cmd!r}')
    expect(ref_at != -1, 'participant_job.sh does not push the result branch')
    expect(
        content_at < ref_at,
        'participant_job.sh publishes the result branch before the content',
    )
    expect(
        'flock' in script[script.rfind('\n', 0, ref_at) : ref_at],
        'the result-branch push is not serialized with flock',
    )
    print('  content push precedes the flock-serialized result-branch push')


def check_content_reached_the_endpoint(babs_proj, provider, endpoint=None):
    """The annexed zips must be *in* the endpoint, not merely referenced.

    `endpoint` is where the endpoint lives on this filesystem. For
    `remote-git` the project knows it only as an ssh URL -- which is the point
    of that provider -- but the test drives an ssh host that points back here,
    so the objects can still be counted directly.
    """
    if provider in ('bare-git', 'worktree-git', 'remote-git'):
        repo = Path(endpoint if endpoint is not None else babs_proj.output_git_url)
        # a repository with a worktree keeps its annex under `.git/`
        annex = repo / 'annex' if (repo / 'annex').is_dir() else repo / '.git' / 'annex'
        objects = list((annex / 'objects').rglob('*.zip'))
        expect(
            len(objects) >= len(SUBJECTS),
            f'Expected >= {len(SUBJECTS)} annexed zips in the git output remote, '
            f'found {len(objects)}',
        )
        print(f'  annexed objects in the git output remote: {len(objects)}')
    else:
        store = Path(babs_proj.output_ria_path)
        objects = list(store.rglob('*.zip'))
        expect(
            len(objects) >= len(SUBJECTS),
            f'Expected >= {len(SUBJECTS)} annexed zips in the output RIA store, '
            f'found {len(objects)}',
        )
        print(f'  annexed objects in the output RIA store: {len(objects)}')


def check_worktree_shows_the_results(endpoint):
    """The point of the worktree provider: results readable in place.

    `receive.denyCurrentBranch=updateInstead` makes the receiver check out
    what was pushed, so after `babs merge` pushes the merge commit the zips
    are visible in the directory itself -- no clone needed.
    """
    print('\n== The receiving worktree itself shows the merged results')
    for sub in SUBJECTS:
        name = zip_name(sub)
        expect(
            (endpoint / name).is_symlink() or (endpoint / name).exists(),
            f'{name} is not present in the receiving worktree {endpoint}',
        )
    print(f'  {endpoint} lists: {sorted(p.name for p in endpoint.glob("*.zip"))}')


def check_fresh_clone_can_retrieve(babs_proj, clone_path):
    """The real acceptance test: a fresh clone can `datalad get` the results."""
    print(f'\n== Cloning the output remote into {clone_path} and retrieving results')
    source = babs_proj.output_remote.clone_source(
        babs_proj.output_git_url, babs_proj.analysis_dataset_id
    )
    run(['datalad', 'clone', source, str(clone_path)])
    for sub in SUBJECTS:
        name = zip_name(sub)
        expect((clone_path / name).is_symlink() or (clone_path / name).exists(), f'{name} missing')
        run(['datalad', 'get', name], cwd=clone_path)
        resolved = (clone_path / name).resolve()
        expect(resolved.exists(), f'`datalad get {name}` did not materialize content')
        with zipfile.ZipFile(clone_path / name) as zf:
            names = zf.namelist()
        wanted = f'{ZIP_FOLDERNAME}/{sub}/anat/{sub}_T1w.json'
        expect(wanted in names, f'{name} does not contain {wanted}; it holds {names}')
        print(f'  {name}: {len(names)} entries, including {wanted}')


# ---------------------------------------------------------------------------
# one full run
# ---------------------------------------------------------------------------
def run_provider(
    provider, workdir, shim_dir, bids_path, container_path, concurrent=False, ssh_host=None
):
    print('\n' + '=' * 78)
    print(f'== PROVIDER: {provider}')
    print('=' * 78)

    root = workdir / provider
    root.mkdir(parents=True, exist_ok=True)
    scratch = root / 'scratch'
    scratch.mkdir()
    project_root = root / 'babs_project'
    config_path = write_container_config(
        root / 'container_config.yaml', bids_path, shim_dir, scratch
    )

    init_cmd = [
        *BABS_CLI,
        'init',
        str(project_root),
        '--container_ds',
        str(container_path),
        '--container_name',
        CONTAINER_NAME,
        '--container_config',
        str(config_path),
        '--processing_level',
        'subject',
        '--queue',
        'slurm',
    ]
    endpoints = {
        'bare-git': root / 'output.git',
        'worktree-git': root / 'output',
        'remote-git': root / 'output.git',
    }
    endpoint = endpoints.get(provider)
    if provider == 'remote-git':
        # BABS validates this one instead of creating it, so it has to exist
        # and be a git-annex repository before `babs init` runs.
        print(f'\n== Preparing the ssh endpoint {endpoint} (BABS will not create it)')
        run(['git', 'init', '--bare', '-q', str(endpoint)])
        run(['git', 'annex', 'init', 'e2e-endpoint'], cwd=endpoint)
        init_cmd += ['--output-remote', f'ssh://{ssh_host}{endpoint}']
    elif endpoint is not None:
        init_cmd += ['--output-remote', f'file://{endpoint}']

    print('\n== babs init')
    run(init_cmd)

    analysis_path = project_root / 'analysis'

    # The recorded project config must say what we asked for -- and, for the
    # default, must say nothing at all, so that pre-existing projects (whose
    # config also says nothing) keep working.
    import yaml

    proj_config = yaml.safe_load((analysis_path / 'code' / 'babs_proj_config.yaml').read_text())
    if provider == 'ria':
        expect(
            'output_remote' not in proj_config,
            'The RIA default must not record an `output_remote` section '
            '(so that "absent" unambiguously means "the default").',
        )
    else:
        expected_url = (
            f'ssh://{ssh_host}{endpoint}' if provider == 'remote-git' else f'file://{endpoint}'
        )
        # Only the url is recorded: the provider is derived from it.
        expect(
            proj_config.get('output_remote') == {'url': expected_url},
            f'Unexpected output_remote in project config: {proj_config.get("output_remote")}',
        )
        expect(endpoint.is_dir(), f'the output endpoint {endpoint} does not exist')
        if provider == 'worktree-git':
            for key, value in (
                ('receive.denyNonFastforwards', 'true'),
                ('receive.denyCurrentBranch', 'updateInstead'),
            ):
                actual = subprocess.run(
                    ['git', '-C', str(endpoint), 'config', '--get', key],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
                expect(actual == value, f'{key} is {actual!r} at {endpoint}, expected {value!r}')

    print('\n== babs check-setup')
    run([*BABS_CLI, 'check-setup', str(project_root)])

    from babs.base import BABS

    babs_proj = BABS(project_root)
    babs_proj.wtf_key_info()
    print(f'  output remote: {babs_proj.output_remote!r}')
    print(f'  git endpoint : {babs_proj.output_git_url}')
    expect(
        babs_proj.output_remote.type == provider,
        f'Rehydrated provider is {babs_proj.output_remote.type}, expected {provider}',
    )
    expect(
        check_result_branches(babs_proj, 0) == [],
        'A freshly initialized project must have no result branches',
    )

    check_two_phase_publication(analysis_path, babs_proj)

    write_job_submit_csv(analysis_path)
    task_ids = list(range(1, len(SUBJECTS) + 1))
    if concurrent:
        # Both jobs publish content into the same endpoint at the same time,
        # and only the result-branch push is serialized by `flock`.
        print(f'\n-- Running {len(task_ids)} participant jobs concurrently')
        with ThreadPoolExecutor(max_workers=len(task_ids)) as pool:
            futures = [pool.submit(run_participant_job, analysis_path, t) for t in task_ids]
            for future in futures:
                future.result()
    else:
        for task_id in task_ids:
            run_participant_job(analysis_path, task_id)

    print('\n== After the jobs: both channels must have reached the endpoint')
    branches = check_result_branches(babs_proj, len(SUBJECTS))
    print(f'  result branches: {branches}')
    check_content_reached_the_endpoint(babs_proj, provider, endpoint)

    print('\n== babs status')
    run([*BABS_CLI, 'status', str(project_root)])
    from babs.status import read_job_status_csv

    statuses = read_job_status_csv(babs_proj.job_status_path_abs)
    with_results = sorted(key[0] for key, job in statuses.items() if job.has_results)
    expect(
        with_results == SUBJECTS,
        f'`babs status` should see results for {SUBJECTS}, it saw {with_results}',
    )
    print(f'  babs status sees results for: {with_results}')

    print('\n== babs merge')
    run([*BABS_CLI, 'merge', str(project_root)])

    print('\n== After the merge: result branches are gone from the endpoint')
    check_result_branches(babs_proj, 0)

    if provider == 'worktree-git':
        check_worktree_shows_the_results(endpoint)

    check_fresh_clone_can_retrieve(babs_proj, root / 'verify_clone')

    print(f'\n== PROVIDER {provider}: OK')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--provider',
        choices=['ria', 'bare-git', 'worktree-git', 'remote-git', 'both'],
        default='both',
        help='Which output remote(s) to exercise.',
    )
    parser.add_argument(
        '--ssh-host',
        help='ssh host (typically a `Host` alias from ~/.ssh/config) that can reach this '
        'filesystem, enabling the `remote-git` provider. Without it that provider is '
        'skipped, since it needs a real git transport.',
    )
    parser.add_argument('--workdir', help='Directory to work in (a temp dir by default).')
    parser.add_argument('--keep', action='store_true', help='Do not delete the work directory.')
    parser.add_argument(
        '--concurrent',
        action='store_true',
        help='Run the participant jobs in parallel, as a real array job would.',
    )
    args = parser.parse_args(argv)

    if args.workdir:
        workdir = Path(args.workdir).absolute()
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        workdir = Path(tempfile.mkdtemp(prefix='babs-e2e-'))
    print(f'Work directory: {workdir}')
    assert_using_this_tree()

    providers = ['ria', 'bare-git', 'worktree-git'] if args.provider == 'both' else [args.provider]
    if args.provider == 'both' and args.ssh_host:
        # only with an ssh host to point it at: it is the one provider that
        # needs a real git transport rather than a path
        providers.append('remote-git')
    if 'remote-git' in providers and not args.ssh_host:
        print('The `remote-git` provider needs --ssh-host; skipping it.', file=sys.stderr)
        providers.remove('remote-git')
    try:
        shim_dir = write_shims(workdir / 'shims')
        bids_path = make_bids_dataset(workdir / 'ds000003-demo')
        container_path = make_container_dataset(workdir / 'containers')
        for provider in providers:
            run_provider(
                provider,
                workdir,
                shim_dir,
                bids_path,
                container_path,
                concurrent=args.concurrent,
                ssh_host=args.ssh_host,
            )
    except E2EFailure as exc:
        print(f'\nE2E FAILED:\n{exc}', file=sys.stderr)
        print(f'Work directory kept for inspection: {workdir}', file=sys.stderr)
        return 1
    if not args.keep and not args.workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    print('\nAll providers passed: ' + ', '.join(providers))
    return 0


if __name__ == '__main__':
    sys.exit(main())
