Migrating to tutor-objectstorage
=================================

``tutor-objectstorage`` can replace two different plugins. Which recipe
below applies depends on which one you're running:

- **From tutor-minio** (the most common case — this was the plugin bundled
  with Tutor's own binary releases for years): a **real data migration**.
  MinIO was archived upstream; this plugin's ``rustfs`` backend runs RustFS
  instead, a from-scratch Rust reimplementation of the S3 API, not a fork or
  drop-in replacement for MinIO's on-disk format. It uses a different
  bind-mount path too (``data/rustfs``, not ``data/minio``). There is no
  documented or supported way to point RustFS at a MinIO data directory and
  have it read the existing objects, so every object has to actually move,
  bucket by bucket, over the S3 API itself — see `From tutor-minio`_ below.
- **From tutor-rustfs** (this plugin's own direct predecessor): its service
  names, bind-mount path, and PVC name are kept **identical** in this
  plugin, so migrating is a config-key rename, not a data migration: no new
  volume, no new chown dance, no re-upload.

Tutor itself has no built-in mechanism for renaming a plugin's own config
keys (confirmed by reading ``tutor/config.py``: the only rename support is a
fixed, non-extensible list for Tutor core's *own* historical renames), so
this has to be a documented manual step — the recipes below are copy-pasteable,
not a script you run blind.

.. warning::

   **Copy your existing generated secret across explicitly, into BOTH keys
   below — do not skip this and do not set only one.**

   ::

       --set OBJSTORAGE_AWS_SECRET_ACCESS_KEY=<your captured old secret>
       --set OPENEDX_AWS_SECRET_ACCESS_KEY=<your captured old secret>

   ``CONFIG_UNIQUE`` values (``OBJSTORAGE_AWS_SECRET_ACCESS_KEY``) are only
   generated if the key is **absent**, so setting that one alone looks like
   it should be enough — it isn't. ``tutor plugins enable objectstorage``
   has already run by the time you reach this step, and that run rendered
   ``OPENEDX_AWS_SECRET_ACCESS_KEY`` (a ``CONFIG_OVERRIDES`` entry, a plain
   Jinja reference to ``OBJSTORAGE_AWS_SECRET_ACCESS_KEY``) against whatever
   secret was freshly, randomly generated at that moment — then froze the
   *result* into ``config.yml`` as a literal string. Changing
   ``OBJSTORAGE_AWS_SECRET_ACCESS_KEY`` afterwards does not reach that frozen
   literal; it does not get re-rendered. Confirmed live: setting only
   ``OBJSTORAGE_AWS_SECRET_ACCESS_KEY`` in the recipes below left
   ``tutor config printvalue OPENEDX_AWS_SECRET_ACCESS_KEY`` showing the new
   random secret, not the captured old one. Setting
   ``OPENEDX_AWS_SECRET_ACCESS_KEY`` explicitly, in the same ``tutor config
   save`` call, pins it as its own literal and fixes this.

   If you skip this, the object storage server still works (it just starts
   with a new credential), but any presigned URL computed with the old
   secret becomes invalid immediately, and any client or script with the old
   secret hardcoded starts failing with ``SignatureDoesNotMatch``.

From ``tutor-minio``
---------------------

This is a **real data migration**, not a rename — see the explanation
above. The recipe moves every object over the S3 API itself, through a
local staging directory, so it works regardless of which backend you land
on (``rustfs``, ``azure``, or ``external_s3``) and needs no direct network
path between the old and new containers.

This assumes the common case: ``MINIO_GATEWAY`` is unset (``null``), i.e.
MinIO is running as a real local server with its own bind-mounted volume,
not MinIO's old (and now-removed-upstream) "gateway" mode in front of a
real S3 or Azure account. If you *are* running gateway mode, your objects
already live in that external account, not in MinIO's volume — skip the
dump/restore steps below and configure that backend directly instead: the
``azure`` backend config in the README if your gateway target was Azure, or
the ``external_s3`` config if it was S3-compatible, pointed at that same
external account. Tutor-minio never actually surfaced the gateway target's
credentials as its own config keys, so that path was already a manual,
undocumented override before this plugin existed.

::

    # 1. Capture the old plugin's values first. Do this before touching
    #    anything else — step 3 disables the minio plugin, which removes
    #    its container from the environment.
    tutor config printvalue MINIO_BUCKET_NAME
    tutor config printvalue MINIO_FILE_UPLOAD_BUCKET_NAME
    tutor config printvalue MINIO_VIDEO_UPLOAD_BUCKET_NAME
    tutor config printvalue MINIO_GRADES_BUCKET_NAME
    tutor config printvalue MINIO_OPENEDX_LEARNING_BUCKET_NAME
    tutor config printvalue MINIO_DISCOVERY_BUCKET_NAME
    tutor config printvalue MINIO_QUERYSTRING_AUTH
    tutor config printvalue MINIO_HOST
    tutor config printvalue MINIO_GATEWAY            # confirm this prints "null" (confirmed live)
    tutor config printvalue MINIO_AWS_SECRET_ACCESS_KEY
    tutor config printvalue OPENEDX_AWS_ACCESS_KEY    # almost always "openedx"

    # 2. With the minio container still running, dump every bucket to a
    #    local staging directory. This uses a disposable `mc` container on
    #    the same Docker network as your deployment, so it works the same
    #    way whether you're on a laptop or a remote server — no host port
    #    needs to be published. Substitute your own captured bucket names
    #    and secret for the placeholders below; the project name
    #    (`tutor_local` by default) is the `--project-name` value your
    #    `tutor local` commands already print at the top of their output.
    mkdir -p ~/minio-migration-dump
    docker run --rm --network tutor_local_default \
        -v ~/minio-migration-dump:/dump \
        --entrypoint sh docker.io/rancher/mirrored-minio-mc:RELEASE.2022-05-09T04-08-26Z -e -c '
    mc config host add minio http://minio:9000 openedx <your captured MINIO_AWS_SECRET_ACCESS_KEY> --api s3v4
    for b in openedx openedxuploads openedxvideos openedxgrades openedxlearning; do
      mkdir -p /dump/$b
      mc mirror --overwrite minio/$b /dump/$b
    done'
    # If MINIO_DISCOVERY_BUCKET_NAME printed a value in step 1, add it to
    # the `for b in ...` list above too.

    #    Spot-check what landed before going further:
    find ~/minio-migration-dump -type f | wc -l

    # 3. Swap the plugin.
    tutor local stop minio
    pip install tutor-objectstorage
    tutor plugins disable minio
    tutor plugins enable objectstorage
    pip uninstall tutor-minio   # optional, once you've confirmed the new plugin works

    # 4. Re-apply the captured values under the new names (bucket names and
    #    QUERYSTRING_AUTH lose the MINIO_ infix; HOST becomes
    #    OBJSTORAGE_RUSTFS_HOST if you're moving to the rustfs backend —
    #    swap in OBJSTORAGE_AZURE_HOST/OBJSTORAGE_EXTERNAL_S3_* instead if
    #    you're moving to a different backend). Use your own printed
    #    values, not these examples — and see the warning above: both
    #    secret keys below are mandatory, not redundant.
    tutor config save \
        --set OBJSTORAGE_BACKEND=rustfs \
        --set OBJSTORAGE_BUCKET_NAME=openedx \
        --set OBJSTORAGE_FILE_UPLOAD_BUCKET_NAME=openedxuploads \
        --set OBJSTORAGE_VIDEO_UPLOAD_BUCKET_NAME=openedxvideos \
        --set OBJSTORAGE_GRADES_BUCKET_NAME=openedxgrades \
        --set OBJSTORAGE_OPENEDX_LEARNING_BUCKET_NAME=openedxlearning \
        --set OBJSTORAGE_QUERYSTRING_AUTH=true \
        --set OBJSTORAGE_RUSTFS_HOST=files.example.com \
        --set OBJSTORAGE_AWS_SECRET_ACCESS_KEY=<your captured MINIO_AWS_SECRET_ACCESS_KEY> \
        --set OPENEDX_AWS_SECRET_ACCESS_KEY=<your captured MINIO_AWS_SECRET_ACCESS_KEY>

    tutor config printvalue OPENEDX_AWS_SECRET_ACCESS_KEY
    # Compare against the MINIO_AWS_SECRET_ACCESS_KEY you printed in step 1.

    # 5. Create the data directory (fresh Linux installs only — see the
    #    README's rustfs section), then launch: this starts rustfs and runs
    #    its init task, which creates the (still-empty) buckets.
    mkdir -p "$(tutor config printroot)/data/rustfs"
    sudo chown -R 10001:10001 "$(tutor config printroot)/data/rustfs"   # Linux only
    tutor local launch

    # 6. Restore the dump into the new buckets, same pattern as step 2 but
    #    reversed, against rustfs's own CLI image instead of mc. If you
    #    renamed any bucket in step 4, use the NEW name as the destination
    #    here — the dump directory itself still uses the OLD names from
    #    step 2, since that's what you captured.
    docker run --rm --network tutor_local_default \
        -v ~/minio-migration-dump:/dump:ro \
        --entrypoint sh docker.io/rustfs/rc:v0.1.36 -e -c '
    rc alias set rustfs http://rustfs:9000 openedx <your captured MINIO_AWS_SECRET_ACCESS_KEY>
    for b in openedx openedxuploads openedxvideos openedxgrades openedxlearning; do
      rc mirror /dump/$b rustfs/$b
    done'

    # 7. Verify the objects actually arrived, then clean up the staging
    #    directory. `rc ls --recursive` against each bucket should match
    #    what you dumped in step 2 - compare against
    #    `find ~/minio-migration-dump -type f | wc -l` from step 2, or diff
    #    checksums for anything you want to double-check byte-for-byte:
    #        shasum ~/minio-migration-dump/<bucket>/<path> and compare by
    #        hand against the restored object.
    docker run --rm --network tutor_local_default \
        --entrypoint sh docker.io/rustfs/rc:v0.1.36 -e -c '
    rc alias set rustfs http://rustfs:9000 openedx <your captured MINIO_AWS_SECRET_ACCESS_KEY>
    for b in openedx openedxuploads openedxvideos openedxgrades openedxlearning; do
      rc ls --recursive rustfs/$b
    done'
    tutor local do init --limit=objectstorage   # idempotent; re-running is safe
    rm -rf ~/minio-migration-dump

.. note::

   **Moving to a different destination backend** (``azure`` or
   ``external_s3`` instead of ``rustfs``): steps 1-2 are identical; in step
   4, set ``OBJSTORAGE_BACKEND`` and that backend's own config keys instead
   (see the README) and skip the rustfs-specific ``mkdir``/``chown`` in step
   5; in step 6, point the restore step's alias at that backend's endpoint
   instead of ``rustfs:9000`` (``external_s3`` has no local container at
   all — point directly at your provider's endpoint, and create the
   buckets yourself first, same as any fresh ``external_s3`` setup).

.. note::

   **A security improvement, not a regression, if you notice it**: this
   plugin's init task grants the public bucket anonymous *read* access
   (``rc anonymous set download``), not MinIO's old anonymous *read-and-write*
   policy (``mc policy set public``) — see the comment in
   ``tutorobjectstorage/templates/objectstorage/tasks/objectstorage/rustfs.sh``.
   Nothing you did in this migration relies on write access to that bucket
   being anonymous, so this tightens access without changing behavior
   anyone should be depending on.

**Verified by hand**: this exact recipe was run end-to-end against a
throwaway deployment — real tutor-minio buckets populated with files
resembling course assets, a profile image, a video upload, a grades export,
and a learning-core block, dumped, swapped to the ``rustfs`` backend, and
restored — with every object's SHA-1 checksum confirmed identical before and
after.

From ``tutor-rustfs``
----------------------

::

    # 1. Capture the old plugin's values first.
    tutor config printvalue RUSTFS_BUCKET_NAME
    tutor config printvalue RUSTFS_FILE_UPLOAD_BUCKET_NAME
    tutor config printvalue RUSTFS_VIDEO_UPLOAD_BUCKET_NAME
    tutor config printvalue RUSTFS_GRADES_BUCKET_NAME
    tutor config printvalue RUSTFS_OPENEDX_LEARNING_BUCKET_NAME
    tutor config printvalue RUSTFS_QUERYSTRING_AUTH
    tutor config printvalue RUSTFS_HOST
    tutor config printvalue RUSTFS_CONSOLE_HOST
    tutor config printvalue RUSTFS_REGION
    tutor config printvalue RUSTFS_DOCKER_IMAGE
    tutor config printvalue RUSTFS_RC_DOCKER_IMAGE
    tutor config printvalue RUSTFS_UID
    tutor config printvalue RUSTFS_GID
    tutor config printvalue RUSTFS_AWS_SECRET_ACCESS_KEY

    # 2. Swap the plugin.
    pip install tutor-objectstorage
    tutor plugins disable rustfs
    tutor plugins enable objectstorage
    pip uninstall tutor-rustfs   # optional, once you've confirmed the new plugin works

    # 3. Re-apply the captured values under the new names. Bucket names and
    #    QUERYSTRING_AUTH lose the RUSTFS_ infix (they're shared across all
    #    three backends now); everything else becomes OBJSTORAGE_RUSTFS_*.
    #    Use the actual values you printed in step 1 below, not these
    #    examples.
    tutor config save \
        --set OBJSTORAGE_BACKEND=rustfs \
        --set OBJSTORAGE_BUCKET_NAME=openedx \
        --set OBJSTORAGE_FILE_UPLOAD_BUCKET_NAME=openedxuploads \
        --set OBJSTORAGE_VIDEO_UPLOAD_BUCKET_NAME=openedxvideos \
        --set OBJSTORAGE_GRADES_BUCKET_NAME=openedxgrades \
        --set OBJSTORAGE_OPENEDX_LEARNING_BUCKET_NAME=openedxlearning \
        --set OBJSTORAGE_QUERYSTRING_AUTH=true \
        --set OBJSTORAGE_RUSTFS_HOST=files.example.com \
        --set OBJSTORAGE_RUSTFS_CONSOLE_HOST=rustfs.example.com \
        --set OBJSTORAGE_RUSTFS_REGION=us-east-1 \
        --set OBJSTORAGE_RUSTFS_DOCKER_IMAGE=docker.io/rustfs/rustfs:1.0.0 \
        --set OBJSTORAGE_RUSTFS_RC_DOCKER_IMAGE=docker.io/rustfs/rc:v0.1.36 \
        --set OBJSTORAGE_RUSTFS_UID=10001 \
        --set OBJSTORAGE_RUSTFS_GID=10001 \
        --set OBJSTORAGE_AWS_SECRET_ACCESS_KEY=<your captured RUSTFS_AWS_SECRET_ACCESS_KEY> \
        --set OPENEDX_AWS_SECRET_ACCESS_KEY=<your captured RUSTFS_AWS_SECRET_ACCESS_KEY>

    # 4. Confirm the secret matches BEFORE relaunching — see the warning
    #    above if this doesn't match: it means the OPENEDX_ key above was
    #    missed, not that something else is wrong.
    tutor config printvalue OPENEDX_AWS_SECRET_ACCESS_KEY
    # Compare against the RUSTFS_AWS_SECRET_ACCESS_KEY you printed in step 1.

    tutor local launch   # reuses the existing `rustfs` bind-mounted volume unchanged

``RUSTFS_DISCOVERY_BUCKET_NAME`` has no separate step: it's computed from
whether the ``discovery`` plugin is enabled, exactly as before, just now
under the shared ``OBJSTORAGE_DISCOVERY_BUCKET_NAME`` name.

From ``tutor-contrib-s3`` (optional — not a required migration)
-------------------------------------------------------------------

``tutor-contrib-s3`` is an independently maintained plugin and is not
superseded by this one — nothing requires you to move off it. If you want
to anyway, for the convenience of one unified plugin across your whole
Open edX footprint, the key mapping is:

.. list-table::
   :header-rows: 1

   * - ``tutor-contrib-s3`` key
     - ``tutor-objectstorage`` key
   * - ``S3_HOST``
     - ``OBJSTORAGE_EXTERNAL_S3_ENDPOINT_HOST``
   * - ``S3_PORT``
     - ``OBJSTORAGE_EXTERNAL_S3_PORT``
   * - ``S3_REGION``
     - ``OBJSTORAGE_EXTERNAL_S3_REGION``
   * - ``S3_USE_SSL``
     - ``OBJSTORAGE_EXTERNAL_S3_USE_SSL``
   * - ``S3_STORAGE_BUCKET``
     - ``OBJSTORAGE_BUCKET_NAME`` (shared across all backends now)
   * - ``S3_FILE_UPLOAD_BUCKET``
     - ``OBJSTORAGE_FILE_UPLOAD_BUCKET_NAME`` (shared)
   * - ``S3_GRADE_BUCKET``
     - ``OBJSTORAGE_GRADES_BUCKET_NAME`` (shared)
   * - ``S3_PROFILE_IMAGE_BUCKET``
     - ``OBJSTORAGE_EXTERNAL_S3_PROFILE_IMAGE_BUCKET`` (same empty-means-
       local-filesystem behavior; defaults to the shared bucket instead of
       empty)
   * - ``S3_PROFILE_IMAGE_CUSTOM_DOMAIN``
     - ``OBJSTORAGE_EXTERNAL_S3_PROFILE_IMAGE_CUSTOM_DOMAIN``
   * - ``S3_ADDRESSING_STYLE``
     - ``OBJSTORAGE_EXTERNAL_S3_ADDRESSING_STYLE``
   * - ``S3_CUSTOM_DOMAIN``
     - ``OBJSTORAGE_EXTERNAL_S3_CUSTOM_DOMAIN``
   * - ``OPENEDX_AWS_ACCESS_KEY`` / ``OPENEDX_AWS_SECRET_ACCESS_KEY``
     - Same names, still set by hand — this plugin's ``external_s3``
       backend contributes nothing to them either, exactly like
       ``tutor-contrib-s3``.

**This is a real behavior change, not just a rename**: this plugin's
bucket model is the fuller 6-bucket one (adds video-upload and
openedx-learning buckets, which ``tutor-contrib-s3`` never had), so you'll
need to create 2 more buckets. ``S3_SIGNATURE_VERSION``,
``S3_DEFAULT_ACL``, and ``S3_REQUEST_CHECKSUM_CALCULATION`` are no longer
configurable — they're hardcoded to the same values ``tutor-contrib-s3``
already defaults to (``s3v4``, unset/``None``, ``when_required``), so this
should be a no-op for anyone who never changed them away from the default.
