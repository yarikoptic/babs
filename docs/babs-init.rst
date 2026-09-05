##################################################
``babs init``: Initialize a BABS project
##################################################

.. contents:: Table of Contents

**********************
Command-Line Arguments
**********************

.. argparse::
   :ref: babs.cli._parse_init
   :prog: babs init

   --keep_if_failed, --keep-if-failed : @after
      These words below somehow refuse to appear in the built docs...
      Please refer to below section
      here: :ref:`what-if-babs-init-fails` for details.


********************
Detailed description
********************

-------------------------------------------------------------------------
How do I prepare the input dataset, container, and container's YAML file?
-------------------------------------------------------------------------

Please see document :doc:`preparation` for how to prepare these inputs.

.. _how-to-define-name-of-input-dataset:

-----------------------------------------------------------
How do I define the input datasets in the YAML config file?
-----------------------------------------------------------

Please see document :doc:`preparation_config_yaml_file` for how to define the input datasets in the YAML config file.

------------------------------------------------------
How is the list of subjects (and sessions) determined?
------------------------------------------------------
A list of subjects (and sessions) will be determined when running ``babs init``,
and will be saved in a CSV file called named ``processing_inclusion.csv`` 
located at ``/path/to/my_BABS_project/analysis/code``.

**To filter subjects and sessions**, use ``babs init`` with ``-- /path/to/subject/list/csv/file``. 
Examples: `Single-session example <https://github.com/PennLINC/babs/blob/ba32e8fd2d6473466d3c33a1b17dfffc4438d541/notebooks/initial_sub_list_single-ses.csv>`_, `Multi-session example <https://github.com/PennLINC/babs/blob/ba32e8fd2d6473466d3c33a1b17dfffc4438d541/notebooks/initial_sub_list_multi-ses.csv>`_.

See :ref:`list_included_subjects` for how this list is determined.

.. _what-if-babs-init-fails:

----------------------------
What if ``babs init`` fails?
----------------------------

If ``babs init`` fails, by default it will remove ("clean up") the created, failed BABS project.

When this happens, if you hope to use ``babs check-setup`` to debug what's wrong, you'll notice that
the failed BABS project has been cleaned and it's not ready to run ``babs check-setup`` yet. What you need
to do are as follows:

#. Run ``babs init`` with ``--keep-if-failed`` turned on.

    * In this way, the failed BABS project will be kept.

#. Then you can run ``babs check-setup`` for diagnosis.
#. After you know what's wrong, please remove the failed BABS project
   with following commands::

    cd <project_root>/analysis    # replace `<project_root>` with the path to your BABS project

    # Remove input dataset(s) one by one:
    datalad remove -d inputs/data/<input_ds_name>   # replace `<input_ds_name>` with each input dataset's name
    # repeat above step until all input datasets have been removed.
    # if above command leads to "drop impossible" due to modified content, add `--reckless modification` at the end

    git annex dead here
    datalad push --to input
    datalad push --to output

    cd ..
    pwd   # this prints `<project_root>`; you can copy it in case you forgot
    cd ..   # outside of `<project_root>`
    rm -rf <project_root>

   If you don't remove the failed BABS project, you cannot overwrite it by running ``babs init`` again.


****************
Example commands
****************

Example ``babs init`` command for toy BIDS App + multi-session data on
a SLURM cluster:

.. code-block:: bash

    babs init \
        --container_ds /path/to/toybidsapp-container \
        --container_name toybidsapp-0-0-7 \
        --container_config /path/to/container_toybidsapp.yaml \
        --processing_level session \
        --queue slurm \
        /path/to/a/folder/holding/BABS/project/my_BABS_project

.. note::
    **Throttling SLURM array jobs**: If you want to limit the number of simultaneously running
    array tasks, you can add the ``--throttle`` option. For example, ``--throttle 10`` will
    limit SLURM to run at most 10 array tasks at the same time. This is useful when you have
    many jobs but want to avoid overwhelming the cluster or hitting resource limits.
    The throttle value will be added to the array specification as ``%<throttle>``,
    e.g., ``--array=1-${max_array}%10``.

.. note::
    **Publishing results somewhere other than an output RIA store**: by default
    ``babs init`` creates an output RIA store inside the BABS project root, and
    results are published to it over two channels -- the result branches go to a
    bare git repository inside the store, and the zipped results go to an ORA
    special remote.

    ``--output-remote`` says where to publish instead, and the shape of the
    value says what kind of endpoint it is:

    ==================================== =========================================
    ``--output-remote``                  what BABS does
    ==================================== =========================================
    (omitted)                            creates an output RIA store in the project
    ``/path/to/store``                   creates/uses a RIA store there
    ``ria+file:///path/to/store``        the same, said explicitly
    ``ria+ssh://host/path/to/store``     a RIA store datalad reaches over ssh
    ``file:///path/to/out.git``          creates/uses a **bare** git repository
    ``file:///path/to/out``              creates/uses a repository **with a worktree**
    ``ssh://…``, ``user@host:path``      an **existing** repository, validated only
    ==================================== =========================================

    An existing endpoint is believed over its name: point ``--output-remote``
    at a directory that already holds a RIA store, a bare repository or a
    repository with a worktree, and BABS uses it as what it is.

    ::

        babs init \
            --container_ds /path/to/container-ds \
            --container_name mriqc-24-0-2 \
            --container_config /path/to/container_mriqc.yaml \
            --processing_level subject \
            --queue slurm \
            --output-remote file:///path/to/output.git \
            /path/to/my_BABS_project

    For a git repository (bare or not), one endpoint carries **both**
    publication channels: the result branches and the annexed zip files. A
    local one is created if it does not exist, and BABS runs ``git annex init``
    in it. That step is not cosmetic: a repository with no ``annex.uuid`` is
    treated by git-annex as a git-only remote, and result *content* is then
    silently not transferred at all, leaving result branches that point at zip
    files stored nowhere.

    A repository **with a worktree** is configured with
    ``receive.denyCurrentBranch=updateInstead`` and
    ``receive.denyNonFastforwards=true``, so pushes into its checked-out branch
    are accepted and update the working tree -- the results become readable in
    place, without a clone. Two consequences: such a push is refused while that
    working tree is dirty, and since jobs only ever push ``job-*`` branches,
    the checkout advances when ``babs merge`` pushes the main branch, not once
    per job.

    A **remote** endpoint (anything BABS cannot reach as a filesystem path) is
    never created: BABS cannot run ``git annex init`` over a git transport, so
    it validates instead. The endpoint must be reachable and must already
    advertise a ``git-annex`` branch, which is what tells an annex-capable host
    (forgejo-aneksajo, GIN, or any repository someone ran ``git annex init``
    in) from a plain git host that would drop the content. Note that the
    compute nodes need their own non-interactive credentials for that endpoint
    -- an ssh key or a token in a git credential helper -- which is site and
    user configuration, not something BABS arranges.

    Everything else is unchanged: ``babs status``, ``babs merge`` and a
    ``datalad clone`` of the endpoint work the same way, and jobs still push
    content first and the result branch last. ``babs check-setup`` re-checks
    the setup either way.

.. note::
    **Shared group permissions**: On multi-user shared filesystems:

    #. **Set ``umask 002``** in your shell startup (for example ``~/.bashrc``). This is
       **necessary** so new files remain group-writable for collaborators.
    #. **Pass ``--shared-group <GROUP>``** to ``babs init`` so Git and RIA stores are
       created with group-shared permissions.

    With ``--shared-group``, BABS writes generated scripts with group read, write,
    and execute permissions (equivalent to mode ``770``), and registers BABS repositories in Git
    ``safe.directory`` so different users in the same Unix group can run
    ``babs status`` and ``babs submit`` without ownership issues.


*********
Debugging
*********

-----------------------------------
Error when cloning an input dataset
-----------------------------------
What happened: After ``babs init`` prints out a message like this:
``Cloning input dataset #x: '/path/to/input_dataset'``, there was an error message that includes this information:
``err: 'fatal: repository '/path/to/input_dataset' does not exist'``.

Diagnosis: This means that the specified path to this input dataset (i.e., in ``origin_url``) was not valid;
there is no DataLad dataset there.

How to solve the problem: Fix this path. To confirm the updated path is valid, you can try cloning
it to a temporary directory with ``datalad clone /updated/path/to/input_dataset``. If it is successful,
you can go ahead rerun ``babs init``.

********
See also
********

* :doc:`preparation`
* :doc:`create_babs_project`
