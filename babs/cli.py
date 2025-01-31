"""This provides command-line interfaces of babs functions"""

import argparse
import os
import os.path as op
import datalad.api as dlapi
import pandas as pd
import yaml
import warnings
from filelock import Timeout, FileLock
# import sys
# from datalad.interface.base import build_doc

# from babs.core_functions import babs_init, babs_submit, babs_status
from babs.utils import (get_datalad_version,
                        validate_type_session,
                        read_job_status_csv,
                        create_job_status_csv)
from babs.babs import BABS, Input_ds, System

# @build_doc
def babs_init_cli():
    """
    Initialize a babs project and bootstrap scripts that will be used later.

    Example command:
    # TODO: to add an example command here!

    """

    parser = argparse.ArgumentParser(
        description="Initialize a babs project and bootstrap scripts that will be used later",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "--where_project", "--where-project",
        help="Absolute path to the directory where the babs project will locate",
        required=True)
    parser.add_argument(
        "--project_name", "--project-name",
        help="The name of the babs project; "
             "this folder will be automatically created in the directory `where_project`.",
        required=True)
    parser.add_argument(
        '--input',
        action='append',   # append each `--input` as a list;
        # will get a nested list: [[<ds_name_1>, <ds_path_1>], [<ds_name_2>, <ds_path_2>]]
        # ref: https://docs.python.org/3/library/argparse.html
        nargs=2,   # expect 2 arguments per `--input` from the command line;
        #            they will be gathered as one list
        metavar=('input_dataset_name', 'input_dataset_path'),
        help="Input datalad dataset. "
             "First argument is a name of this input dataset. "
             "Second argument is the path to this input dataset.",
        required=True)
    parser.add_argument(
        '--list_sub_file', '--list-sub-file',   # optional flag
        type=str,
        help="Path to the CSV file that lists the subject (and sessions) to analyze; "
        " If there is no such file, please not to specify this flag."
        " Single-session data: column of 'sub_id';"
        " Multi-session data: columns of 'sub_id' and 'ses_id'.",)
    parser.add_argument(
        '--container_ds', '--container-ds',
        help="Path to the container datalad dataset",
        required=True)
    parser.add_argument(
        '--container_name', '--container-name',
        help="The name of the BIDS App container, the `NAME` in `datalad containers-add NAME`."
        + " Importantly, this should include the BIDS App's name"
        + " to make sure the bootstrap scripts are set up correctly;"
        + " Also, the version number should be added, too. "
        + " `babs-init` is not case sensitive to this `--container_name`"
        + " Example: `QSIPrep-0-0-0`",
        # ^^ the BIDS App's name is used to determine: e.g., whether needs/details in $filterfile
        required=True)
    parser.add_argument(
        '--container_config_yaml_file', '--container-config-yaml-file',
        help="Path to a YAML file that contains the configurations"
        " of how to run the BIDS App container")
    parser.add_argument(
        "--type_session", "--type-session",
        choices=['single-ses', 'single_ses', 'single-session', 'single_session',
                 'multi-ses', 'multi_ses', 'multiple-ses', 'multiple_ses',
                 'multi-session', 'multi_session', 'multiple-session', 'multiple_session'],
        help="Whether the input dataset is single-session ['single-ses'] "
             "or multiple-session ['multi-ses']",
        required=True)
    parser.add_argument(
        "--type_system",
        choices=["sge", "slurm"],
        help="The name of the job scheduling type_system that you will use. Choices are sge and slurm.",
        required=True)

    return parser

    # args = parser.parse_args()
    # print(args.input)

    # babs_init(args.where_project, args.project_name,
    #           args.input, args.list_sub_file,
    #           args.container_ds,
    #           args.container_name, args.container_config_yaml_file,
    #           args.type_session, args.type_system)


def babs_init_main():
    # def babs_init(where_project, project_name,
    #               input, list_sub_file,
    #               container_ds,
    #               container_name, container_config_yaml_file,
    #               type_session, type_system):
    """
    This is the core function of babs-init.

    Parameters:
    --------------
    where_project: str
        absolute path to the directory where the project will be created
    project_name: str
        the babs project name
    input: nested list
        for each sub-list:
            element 1: name of input datalad dataset (str)
            element 2: path to the input datalad dataset (str)
    list_sub_file: str or None
        Path to the CSV file that lists the subject (and sessions) to analyze;
        or `None` if CLI's flag isn't specified
        single-ses data: column of 'sub_id';
        multi-ses data: columns of 'sub_id' and 'ses_id'
    container_ds: str
        path to the container datalad dataset
    container_name: str
        name of the container, best to include version number.
        e.g., 'fmriprep-0-0-0'
    container_config_yaml_file: str
        Path to a YAML file that contains the configurations
        of how to run the BIDS App container
    type_session: str
        multi-ses or single-ses
    type_system: str
        sge or slurm
    """

    # Get arguments:
    args = babs_init_cli().parse_args()

    where_project = args.where_project
    project_name = args.project_name
    input = args.input
    list_sub_file = args.list_sub_file
    container_ds = args.container_ds
    container_name = args.container_name
    container_config_yaml_file = args.container_config_yaml_file
    type_session = args.type_session
    type_system = args.type_system

    # print datalad version:
    # if no datalad is installed, will raise error
    print("DataLad version: " + get_datalad_version())

    # =================================================================
    # Sanity checks:
    # =================================================================
    project_root = op.join(where_project, project_name)

    # # check if it exists:
    # if op.exists(project_root):
    #     raise Exception("the folder `project_name` already exists in the directory `where_project`!")

    # check if `where_project` is writable:
    if not os.access(where_project, os.W_OK):
        raise Exception("the `where_project` is not writable!")

    # validate `type_session`:
    type_session = validate_type_session(type_session)

    input_ds = Input_ds(input, list_sub_file, type_session)

    # sanity check on the input dataset: the dir should exist, and should be datalad dataset:
    for the_input_ds in input_ds.df["path_in"]:
        if the_input_ds[0:6] == "osf://":  # first 6 char
            pass   # not to check, as cannot be checked by `dlapi.status`
        else:
            _ = dlapi.status(dataset=the_input_ds)
        # ^^ if not datalad dataset, there will be an error saying no installed dataset found
        # if fine, will print "nothing to save, working tree clean"

    # Create an instance of babs class:
    babs_proj = BABS(project_root,
                     type_session,
                     type_system)

    # Validate system's type name `type_system`:
    system = System(type_system)

    # print out key information for visual check:
    print("")
    print("project_root of this BABS project: " + babs_proj.project_root)
    print("type of data of this BABS project: " + babs_proj.type_session)
    print("job scheduling system of this BABS project: " + babs_proj.type_system)
    print("")

    # call method `babs_bootstrap()`:
    babs_proj.babs_bootstrap(input_ds,
                             container_ds, container_name, container_config_yaml_file,
                             system)


def babs_submit_cli():
    """
    Submit jobs.

    Can choose one of these flags:
    --count <number of jobs to submit>  # should be larger than # of `--job`
    --all   # if specified, will submit all remaining jobs that haven't been submitted.
    --job sub-id ses-id   # can repeat

    If none of these flags are specified, will only submit one job.

    Example command:
    # TODO: to add an example command here!
    """

    parser = argparse.ArgumentParser(
        description="Submit jobs that will be run on cluster compute nodes.")
    parser.add_argument(
        "--project_root", "--project-root",
        help="Absolute path to the root of BABS project."
        " For example, '/path/to/my_BABS_project/'.",
        required=True)

    # --count, --job: can only request one of them
    # and none of them are required.
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "--count",
        type=int,
        help="Number of jobs to submit. It should be a positive integer.")
    group.add_argument(
        "--all",
        action='store_true',
        # ^^ if `--all` is specified, args.all = True; otherwise, False
        help="Request to run all jobs that haven't been submitted.")
    group.add_argument(
        "--job",
        action='append',   # append each `--job` as a list;
        nargs='+',
        help="The subject ID (and session ID) whose job to be submitted."
        " Can repeat to submit more than one job."
        " Format would be `--job sub-xx` for single-session dataset,"
        " and `--job sub-xx ses-yy` for multiple-session dataset.")

    return parser

    # args = parser.parse_args()

    # babs_submit(args.project_root,
    #             args.count,  # if not provided, will be `None`
    #             args.job)

def babs_submit_main():
    # def babs_submit(project_root, count=None, job=None):
    """
    This is the core function of `babs-submit`.

    Parameters:
    --------------
    project_root: str
        absolute path to the directory of BABS project
    all: bool
        whether to submit all remaining jobs
    count: int or None
        number of jobs to be submitted
        default: None (did not specify in cli)
            if `--job` is not requested, it will be changed to `1` before going into `babs_submit()`
        any negative int will be treated as submitting all jobs that haven't been submitted.
    job: nested list or None
        For each sub-list, the length should be 1 (for single-ses) or 2 (for multi-ses)
    """

    # Get arguments:
    args = babs_submit_cli().parse_args()

    project_root = args.project_root
    count = args.count
    all = args.all
    job = args.job

    # Get class `BABS` based on saved `analysis/code/babs_proj_config.yaml`:
    babs_proj = get_existing_babs_proj(project_root)

    # Check if this csv file has been created, if not, create it:
    create_job_status_csv(babs_proj)
    # ^^ this is required by the sanity check `check_df_job_specific`

    # Actions on `count`:
    if all:   # if True:
        count = -1  # so that to submit all remaining jobs
    if count is None:
        count = 1   # if not to specify `--count`, change to 1
    # sanity check:
    if count == 0:
        raise Exception("`--count 0` is not valid! Please specify a positive integer. "
                        + "To submit all jobs, please do not specify `--count`.")

    # Actions on `job`:
    if job is not None:
        count = -1    # just in case; make sure all specified jobs will be submitted

        # sanity check:
        if babs_proj.type_session == "single-ses":
            expected_len = 1
        elif babs_proj.type_session == "multi-ses":
            expected_len = 2
        for i_job in range(0, len(job)):
            # expected length in each sub-list:
            assert len(job[i_job]) == expected_len, \
                "There should be " + str(expected_len) + " arguments in `--job`," \
                + " as input dataset(s) is " + babs_proj.type_session + "!"
            # 1st argument:
            assert job[i_job][0][0:4] == "sub-", \
                "The 1st argument of `--job`" + " should be 'sub-*'!"
            if babs_proj.type_session == "multi-ses":
                # 2nd argument:
                assert job[i_job][1][0:4] == "ses-", \
                    "The 2nd argument of `--job`" + " should be 'ses-*'!"

        # turn into a pandas DataFrame:
        if babs_proj.type_session == "single-ses":
            df_job_specified = pd.DataFrame(None,
                                            index=list(range(0, len(job))),
                                            columns=['sub_id'])
        elif babs_proj.type_session == "multi-ses":
            df_job_specified = pd.DataFrame(None,
                                            index=list(range(0, len(job))),
                                            columns=['sub_id', 'ses_id'])
        for i_job in range(0, len(job)):
            df_job_specified.at[i_job, "sub_id"] = job[i_job][0]
            if babs_proj.type_session == "multi-ses":
                df_job_specified.at[i_job, "ses_id"] = job[i_job][1]

        # sanity check:
        df_job_specified = \
            check_df_job_specific(df_job_specified, babs_proj.job_status_path_abs,
                                  babs_proj.type_session, "babs-submit")
    else:  # `job` is None:
        df_job_specified = None

    # Call method `babs_submit()`:
    babs_proj.babs_submit(count, df_job_specified)

def babs_status_cli():
    """
    Check job status.

    Example command:
    # TODO: to add an example command here!
    """

    parser = argparse.ArgumentParser(
        description="Check job status in a BABS project.")
    parser.add_argument(
        "--project_root", "--project-root",
        help="Absolute path to the root of BABS project."
        " For example, '/path/to/my_BABS_project/'.",
        required=True)
    parser.add_argument(
        '--resubmit',
        action='append',   # append each `--resubmit` as a list;
        # ref: https://docs.python.org/3/library/argparse.html
        nargs=1,   # expect 1 argument per `--resubmit` from the command line;
        choices=['failed', 'pending', 'stalled'],
        metavar=('condition to resubmit'),
        help="Under what condition to perform job resubmit. "
             "'failed': the previous submitted job failed "
             "('is_failed' = True in 'job_status.csv'); "
             "'pending': the previous submitted job is pending (without error) in the queue "
             "(example qstat code: 'qw'); "
             "'stalled': the previous submitted job is pending with error in the queue "
             "(example qstat code: 'eqw')."
        )
    parser.add_argument(
        '--resubmit-job',
        action="append",   # append each `--resubmit-job` as a list;
        nargs="+",
        help="The subject ID (and session ID) whose job to be resubmitted."
        " Can repeat to submit more than one job."
        " Currently, this can only resubmit pending, failed, or stalled jobs.")
    # ^^ NOTE: ROADMAP: improve the strategy to deal with `eqw` (stalled) is not to resubmit,
    #                   but fix the issue - Bergman 12/20/22 email
    parser.add_argument(
        '--reckless',
        action='store_true',
        # ^^ if `--reckless` is specified, args.reckless = True; otherwise, False
        help="Whether to resubmit jobs listed in `--resubmit-job`, even they're done or running."
        " WARNING: This hasn't been tested yet!!!")
    parser.add_argument(
        '--container_config_yaml_file', '--container-config-yaml-file',
        help="Path to a YAML file that contains the configurations"
        " of how to run the BIDS App container. It may include 'keywords_alert' section"
        " to be used by babs-status.")
    parser.add_argument(
        '--job_account', '--job-account',
        action='store_true',
        # ^^ if `--job-account` is specified, args.job_account = True; otherwise, False
        help="Whether to account failed jobs, which may take some time."
             " If `--resubmit failed` or `--resubmit-job` for this failed job is also requested,"
             " this `--job-account` will be skipped.")

    return parser

    # args = parser.parse_args()

    # babs_status(args.project_root,
    #             args.resubmit,
    #             args.resubmit_job, args.reckless,
    #             args.container_config_yaml_file,
    #             args.job_account)

def babs_status_main():
    # def babs_status(project_root, resubmit=None,
    #                 resubmit_job=None, reckless=False,
    #                 container_config_yaml_file=None,
    #                 job_account=False):
    """
    This is the core function of `babs-status`.

    Parameters:
    --------------
    project_root: str
        absolute path to the directory of BABS project
    resubmit: nested list or None
        each sub-list: one of 'failed', 'pending', 'stalled'
    resubmit_job: nested list or None
        For each sub-list, the length should be 1 (for single-ses) or 2 (for multi-ses)
    reckless: bool
        Whether to resubmit jobs listed in `--resubmit-job`, even they're done or running
        This is used when `--resubmit-job`
    container_config_yaml_file: str or None
        Path to a YAML file that contains the configurations
        of how to run the BIDS App container.
        It may include 'keywords_alert' section
        to be used by babs-status.
    job_account: bool
        Whether to account failed jobs (e.g., using `qacct` for SGE),
        which may take some time.
    """

    # Get arguments:
    args = babs_status_cli().parse_args()

    project_root = args.project_root
    resubmit = args.resubmit
    resubmit_job = args.resubmit_job
    reckless = args.reckless
    container_config_yaml_file = args.container_config_yaml_file
    job_account = args.job_account

    # Get class `BABS` based on saved `analysis/code/babs_proj_config.yaml`:
    babs_proj = get_existing_babs_proj(project_root)

    # Check if this csv file has been created, if not, create it:
    create_job_status_csv(babs_proj)
    # ^^ this is required by the sanity check `check_df_job_specific`

    # Get the list of resubmit conditions:
    if resubmit is not None:   # user specified --resubmit
        # e.g., [['pending'], ['failed']]
        # change nested list to a simple list:
        flags_resubmit = []
        for i in range(0, len(resubmit)):
            flags_resubmit.append(resubmit[i][0])

        # remove dupliated elements:
        flags_resubmit = list(set(flags_resubmit))   # `list(set())`: acts like "unique"

        # print message:
        print(
            "Will resubmit jobs if "
            + " or ".join(flags_resubmit) + ".")   # e.g., `failed`; `failed or pending`

    else:   # `resubmit` is None:
        print("Did not request resubmit based on job states (no `--resubmit`).")
        flags_resubmit = []   # empty list

    # If `--job-account` is requested:
    if job_account:
        if "failed" not in flags_resubmit:
            print("`--job-account` was requested; `babs-status` may take longer time...")
        else:
            # this is meaningless to run `job-account` if resubmitting anyway:
            print(
                "Although `--job-account` was requested,"
                + " as `--resubmit failed` was also requested,"
                + " it's meaningless to run job account on previous failed jobs,"
                + " so will skip `--job-account`")

    # If `resubmit-job` is requested:
    if resubmit_job is not None:
        # sanity check:
        if babs_proj.type_session == "single-ses":
            expected_len = 1
        elif babs_proj.type_session == "multi-ses":
            expected_len = 2

        for i_job in range(0, len(resubmit_job)):
            # expected length in each sub-list:
            assert len(resubmit_job[i_job]) == expected_len, \
                "There should be " + str(expected_len) + " arguments in `--resubmit-job`," \
                + " as input dataset(s) is " + babs_proj.type_session + "!"
            # 1st argument:
            assert resubmit_job[i_job][0][0:4] == "sub-", \
                "The 1st argument of `--resubmit-job`" + " should be 'sub-*'!"
            if babs_proj.type_session == "multi-ses":
                # 2nd argument:
                assert resubmit_job[i_job][1][0:4] == "ses-", \
                    "The 2nd argument of `--resubmit-job`" + " should be 'ses-*'!"

        # turn into a pandas DataFrame:
        if babs_proj.type_session == "single-ses":
            df_resubmit_job_specific = \
                pd.DataFrame(None,
                             index=list(range(0, len(resubmit_job))),
                             columns=['sub_id'])
        elif babs_proj.type_session == "multi-ses":
            df_resubmit_job_specific = \
                pd.DataFrame(None,
                             index=list(range(0, len(resubmit_job))),
                             columns=['sub_id', 'ses_id'])

        for i_job in range(0, len(resubmit_job)):
            df_resubmit_job_specific.at[i_job, "sub_id"] = resubmit_job[i_job][0]
            if babs_proj.type_session == "multi-ses":
                df_resubmit_job_specific.at[i_job, "ses_id"] = resubmit_job[i_job][1]

        # sanity check:
        df_resubmit_job_specific = \
            check_df_job_specific(df_resubmit_job_specific, babs_proj.job_status_path_abs,
                                  babs_proj.type_session, "babs-status")

        if len(df_resubmit_job_specific) > 0:
            if reckless:    # if `--reckless`:
                print("Will resubmit all the job(s) listed in `--resubmit-job`,"
                      + " even if they're done or running.")
            else:
                print("Will resubmit the job(s) listed in `--resubmit-job`,"
                      + " if they're pending, failed or stalled.")
        else:    # in theory should not happen, but just in case:
            raise Exception("There is no valid job in --resubmit-job!")

    else:   # `--resubmit-job` is None:
        df_resubmit_job_specific = None

    # Call method `babs_status()`:
    babs_proj.babs_status(flags_resubmit, df_resubmit_job_specific, reckless,
                          container_config_yaml_file, job_account)

def get_existing_babs_proj(project_root):
    """
    This is to get `babs_proj` (class `BABS`)
    based on existing yaml file `babs_proj_config.yaml`.
    This should be used by `babs_submit()` and `babs_status`.

    Parameters:
    --------------
    project_root: str
        absolute path to the directory of BABS project
        TODO: accept relative path too, like datalad's `-d`

    Returns:
    --------------
    babs_proj: class `BABS`
        information about a BABS project
    """

    # Sanity check: the path `project_root` exists:
    if op.exists(project_root) is False:
        raise Exception("`--project-root` does not exist! Requested `--project-root` was: "
                        + project_root)

    # Read configurations of BABS project from saved yaml file:
    babs_proj_config_yaml = op.join(project_root,
                                    "analysis/code/babs_proj_config.yaml")
    if op.exists(babs_proj_config_yaml) is False:
        raise Exception("`babs-init` was not successful:"
                        + " there is no 'analysis/code/babs_proj_config.yaml' file!")

    with open(babs_proj_config_yaml) as f:
        babs_proj_config = yaml.load(f, Loader=yaml.FullLoader)
        # ^^ config is a dict; elements can be accessed by `config["key"]["sub-key"]`
    f.close()

    type_session = babs_proj_config["type_session"]
    type_system = babs_proj_config["type_system"]

    # Get the class `BABS`:
    babs_proj = BABS(project_root, type_session, type_system)

    # update key informations including `output_ria_data_dir`:
    babs_proj.wtf_key_info(flag_output_ria_only=True)

    return babs_proj


def check_df_job_specific(df, job_status_path_abs,
                          type_session, which_function):
    """
    This is to perform sanity check on the pd.DataFrame `df`
    which is used by `babs-submit --job` and `babs-status --resubmit-job`.
    Sanity checks include:
    1. Remove any duplicated jobs in requests
    2. Check if requested jobs are part of the inclusion jobs to run

    Parameters:
    ------------------
    df: pd.DataFrame
        i.e., `df_job_specific`
        list of sub_id (and ses_id, if multi-ses) that the user requests to submit or resubmit
    job_status_path_abs: str
        absolute path to the `job_status.csv`
    type_session: str
        'single-ses' or 'multi-ses'
    which_function: str
        'babs-status' or 'babs-submit'
        The warning message will be tailored based on this.

    Returns:
    ----------------
    df: pd.DataFrame
        after removing duplications, if there is

    Notes:
    --------------
    The `job_status.csv` file must present before running this function!
    Please use `create_job_status_csv()` from `utils.py` to create

    TODO:
    -------------
    if `--job-csv` is added in `babs-submit`, update the `which_function`
    so that warnings/error messages are up-to-date (using `--job or --job-csv`)
    """

    # 1. Sanity check: there should not be duplications in `df`:
    df_unique = df.drop_duplicates(keep='first')   # default: keep='first'
    if df_unique.shape[0] != df.shape[0]:
        to_print = "There are duplications in requested "
        if which_function == "babs-submit":
            to_print += "`--job`"
        elif which_function == "babs-status":
            to_print += "`--resubmit-job`"
        else:
            raise Exception("Invalid `which_function`: " + which_function)
        to_print += " . Only the first occuration(s) will be kept..."
        warnings.warn(to_print)

        df = df_unique   # update with the unique one

    # 2. Sanity check: `df` should be a sub-set of all jobs:
    # read the `job_status.csv`:
    lock_path = job_status_path_abs + ".lock"
    lock = FileLock(lock_path)
    try:
        with lock.acquire(timeout=5):  # lock the file, i.e., lock job status df
            df_job = read_job_status_csv(job_status_path_abs)

            # check if `df` is sub-set of `df_job`:
            df_intersection = df.merge(df_job).drop_duplicates()
            # `df_job` should not contain duplications, but just in case..
            # ^^ ref: https://stackoverflow.com/questions/49530918/
            #           check-if-pandas-dataframe-is-subset-of-other-dataframe
            if len(df_intersection) != len(df):
                to_print = "Some of the subjects (and sessions) requested in "
                if which_function == "babs-submit":
                    to_print += "`--job`"
                elif which_function == "babs-status":
                    to_print += "`--resubmit-job`"
                else:
                    raise Exception("Invalid `which_function`: " + which_function)
                to_print += " are not in the final list of included subjects (and sessions)." \
                    + " Path to this final inclusion list is at: " \
                    + job_status_path_abs
                raise Exception(to_print)

    except Timeout:   # after waiting for time defined in `timeout`:
        # if another instance also uses locks, and is currently running,
        #   there will be a timeout error
        print("Another instance of this application currently holds the lock.")

    return df
