Object storage for Open edX
===========================================================

This is a plugin for `Tutor <https://docs.tutor.edly.io>`_ that configures Open
edX's object storage. One config key, ``OBJSTORAGE_BACKEND``, picks which of
three backends actually runs:

.. list-table::
   :header-rows: 1

   * - You want
     - Set ``OBJSTORAGE_BACKEND`` to
   * - An S3 server on your own hardware, alongside Open edX (the default)
     - ``rustfs``
   * - Azure Blob Storage — **read the warning below first**
     - ``azure``
   * - AWS S3, GCS, Ceph, DigitalOcean Spaces, or any other S3-compatible
       provider (including a RustFS/MinIO instance on a separate machine)
     - ``external_s3``

This plugin supersedes two previous, separately-released plugins,
``tutor-rustfs`` and ``tutor-rclone-gateway`` — see `MIGRATING.rst
<MIGRATING.rst>`_ if you're coming from either of those. It is **not**
affiliated with `tutor-contrib-s3 <https://github.com/cleura/tutor-contrib-s3>`_,
an independently maintained plugin; the ``external_s3`` backend here
reimplements equivalent functionality so this plugin can be a complete,
standalone choice, but that project continues to exist on its own and is not
replaced or depended upon by this one.



   **The ``azure`` backend has a known residual risk for very large,
   adversarially-ordered uploads — read the details before relying on it.**

   This plugin's own live conformance suite reproducibly hangs ``rclone serve
   s3`` when multipart upload parts arrive out of ascending order and exceed
   ``OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT`` (2GiB by default). Root-caused
   and narrowed, not eliminated: ordinary, non-adversarial uploads (including
   boto3's own default multipart behavior) were never actually at much risk,
   and the configured buffer can be raised further for deployments with
   larger course videos — see ``EVALUATION.md`` Finding 1.

   Read `EVALUATION.md <EVALUATION.md>`_ before using this backend for
   anything beyond evaluation.





How it works
------------

Whichever backend you pick, Open edX keeps talking to one plain S3 endpoint —
nothing in Django or Studio changes based on ``OBJSTORAGE_BACKEND``. What sits
behind that endpoint differs entirely by backend:

- ``rustfs`` runs `RustFS <https://github.com/rustfs/rustfs>`_, a native
  S3-compatible storage *engine*, as a container alongside Open edX, with its
  own data volume and web console.
- ``azure`` runs two `rclone <https://rclone.org>`_ ``serve s3`` processes (one
  authenticated, one anonymous-read-only) as a *translation layer* in front of
  an Azure Blob Storage account — see `EVALUATION.md <EVALUATION.md>`_ for why
  two processes and the full architectural reasoning.
- ``external_s3`` runs **no local infrastructure at all**. Open edX's S3
  client is pointed directly at your own externally-managed bucket(s); this
  plugin only renders Django settings, nothing else.

Selecting a backend you aren't using costs nothing: that backend's
compose/Kubernetes resources, Caddy routing, and init-task logic simply don't
render. Enabling more than one object-storage plugin (``tutor-minio``, or this
plugin's own predecessors ``tutor-rustfs``/``tutor-rclone-gateway``, or
``tutor-contrib-s3``) raises an error, since they'd collide on the same Open
edX settings.

Installation
------------

::

    pip install tutor-objectstorage
    tutor plugins enable objectstorage

With no further configuration, this reproduces a zero-account ``rustfs``
deployment — ``tutor local launch`` works immediately, the same experience
``tutor-rustfs`` gave on its own.

Testing without external accounts
----------------------------------

The ``rustfs`` backend needs no external account by definition. The ``azure``
backend defaults to `Azurite <https://github.com/Azure/Azurite>`_, Microsoft's
own free local Azure Storage emulator, so it can be evaluated without a real
Azure subscription either (besides the risk acknowledgment above). The
``external_s3`` backend has no local-emulator option — it is, by definition,
pointed at a real external account.

Configuration
--------------

Shared across all three backends:

- ``OBJSTORAGE_BACKEND`` (default: ``"rustfs"``) — ``rustfs``, ``azure``, or
  ``external_s3``.
- ``OBJSTORAGE_BUCKET_NAME`` (default: ``"openedx"``) — also the one bucket
  that is anonymously readable, on the backends that support that
  distinction.
- ``OBJSTORAGE_FILE_UPLOAD_BUCKET_NAME`` (default: ``"openedxuploads"``)
- ``OBJSTORAGE_VIDEO_UPLOAD_BUCKET_NAME`` (default: ``"openedxvideos"``)
- ``OBJSTORAGE_GRADES_BUCKET_NAME`` (default: ``"openedxgrades"``)
- ``OBJSTORAGE_OPENEDX_LEARNING_BUCKET_NAME`` (default: ``"openedxlearning"``)
- ``OBJSTORAGE_DISCOVERY_BUCKET_NAME`` (default: ``"discoveryuploads"`` when
  the ``discovery`` plugin is enabled, otherwise empty) — also anonymously
  readable, for the same reason as the main bucket.
- ``OBJSTORAGE_QUERYSTRING_AUTH`` (default: ``true``)
- ``OPENEDX_AWS_ACCESS_KEY`` / ``OPENEDX_AWS_SECRET_ACCESS_KEY`` — Tutor-core
  settings this plugin wires up automatically for ``rustfs``/``azure``
  (generated, random secret); for ``external_s3`` you set these two yourself.

``rustfs`` backend:

- ``OBJSTORAGE_RUSTFS_HOST`` (default: ``"files.{{ LMS_HOST }}"``)
- ``OBJSTORAGE_RUSTFS_CONSOLE_HOST`` (default: ``"rustfs.{{ LMS_HOST }}"``)
- ``OBJSTORAGE_RUSTFS_REGION`` (default: ``"us-east-1"``)
- ``OBJSTORAGE_RUSTFS_DOCKER_IMAGE`` (default:
  ``"docker.io/rustfs/rustfs:1.0.0"``)
- ``OBJSTORAGE_RUSTFS_RC_DOCKER_IMAGE`` (default:
  ``"docker.io/rustfs/rc:v0.1.36"``) — the init/admin client image.
- ``OBJSTORAGE_RUSTFS_UID`` / ``OBJSTORAGE_RUSTFS_GID`` (both default:
  ``10001``)

RustFS runs as a non-root user and stores data on a bind-mounted volume. On a
fresh Linux install, create the data directory with the right owner *before*
the first ``tutor local start``::

    mkdir -p "$(tutor config printroot)/data/rustfs"
    sudo chown -R 10001:10001 "$(tutor config printroot)/data/rustfs"

(Docker Desktop on macOS/Windows maps bind-mount ownership to the host user,
so this only bites on native Linux — i.e. most production hosts. Kubernetes
handles this automatically via ``fsGroup``.)

``azure`` backend:

- ``OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK`` (default: ``false``) — **required**
  to be ``true`` before any Azure infrastructure renders. See the warning
  above and `EVALUATION.md <EVALUATION.md>`_.
- ``OBJSTORAGE_AZURE_HOST`` (default: ``"files.{{ LMS_HOST }}"``)
- ``OBJSTORAGE_AZURE_REGION`` (default: ``"us-east-1"``)
- ``OBJSTORAGE_AZURE_DOCKER_IMAGE`` (default:
  ``"docker.io/rclone/rclone:1.75.1"``) — **hard-pinned to an exact tag.**
  1.75.1 fixes CVE-2026-88045, an unauthenticated memory-exhaustion
  denial-of-service in rclone's multipart upload path — never go below it.
  If you bump this, re-run the live conformance suite against the new tag
  first.
- ``OBJSTORAGE_AZURE_UID`` / ``OBJSTORAGE_AZURE_GID`` (both default: ``1009``)
- ``OBJSTORAGE_AZURE_ACCOUNT`` / ``OBJSTORAGE_AZURE_ACCOUNT_KEY`` /
  ``OBJSTORAGE_AZURE_ENDPOINT`` — default to Azurite's fixed, publicly
  documented, non-secret values. To use a real Azure Storage account, override
  all three together (``OBJSTORAGE_AZURE_ENDPOINT`` e.g.
  ``https://<account>.blob.core.windows.net``).
- ``OBJSTORAGE_AZURE_AZURITE_DOCKER_IMAGE`` (default:
  ``"mcr.microsoft.com/azure-storage/azurite:3.37.0"``)
- ``OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT`` (default: ``"2G"``) — memory
  ``rclonegw`` will buffer, per in-flight multipart upload, for parts that
  arrive before the part it's actually waiting to write next (the S3 API
  allows parts to arrive in any order). Raised past rclone's own 256MiB
  default because a single out-of-order video upload can legitimately
  exceed that; still a bound, not unlimited (rclone accepts ``"0"`` for
  that, not recommended — see `EVALUATION.md <EVALUATION.md>`_ Finding 1).
  If your course videos routinely exceed this, raise it further, at the
  cost of more worst-case memory per concurrent multipart upload.

There is no console setting for this backend: rclone has no web console or
bucket browser. Use the Azure Portal, Azure Storage Explorer, or
``rclone ncdu`` against the same ``azureblob:`` remote instead.

``external_s3`` backend:

- ``OBJSTORAGE_EXTERNAL_S3_ENDPOINT_HOST`` / ``OBJSTORAGE_EXTERNAL_S3_PORT``
  (both default: ``""``) — leave empty for real AWS S3; set both for any
  other S3-compatible provider.
- ``OBJSTORAGE_EXTERNAL_S3_REGION`` (default: ``""``) — required for real AWS
  S3.
- ``OBJSTORAGE_EXTERNAL_S3_USE_SSL`` (default: ``true``)
- ``OBJSTORAGE_EXTERNAL_S3_ADDRESSING_STYLE`` (default: ``"auto"``) — set to
  ``"path"`` for a Ceph RGW endpoint without ``rgw_dns_name`` configured.
- ``OBJSTORAGE_EXTERNAL_S3_CUSTOM_DOMAIN`` (default: ``""``) — do not set this
  for real AWS S3.
- ``OBJSTORAGE_EXTERNAL_S3_PROFILE_IMAGE_BUCKET`` (default:
  ``"{{ OBJSTORAGE_BUCKET_NAME }}"``) — set to ``""`` to keep profile images
  on local filesystem instead of object storage.
- ``OBJSTORAGE_EXTERNAL_S3_PROFILE_IMAGE_CUSTOM_DOMAIN`` (default: ``""``)

**This backend provisions no buckets automatically** — different providers
have genuinely different ACL/CLI semantics, so, matching how
``tutor-contrib-s3`` already works, you create and access-control every
bucket listed above yourself before enabling this plugin. ``tutor local do
init`` will print exactly which bucket names it expects.

DNS records
-----------

Whichever of ``OBJSTORAGE_RUSTFS_HOST``, ``OBJSTORAGE_RUSTFS_CONSOLE_HOST``, or
``OBJSTORAGE_AZURE_HOST`` applies to your chosen backend must resolve to your
server. ``external_s3`` has no such requirement — it talks to your provider's
own endpoint.

Kubernetes
----------

Both the ``rustfs`` and ``azure`` backends ship full Kubernetes parity
(``tutor k8s ...``). The ``azure`` backend additionally ships a
``NetworkPolicy`` restricting both its gateway Deployments to ingress from the
Caddy pod only — this is not optional hardening; see `EVALUATION.md
<EVALUATION.md>`_. The ``external_s3`` backend creates no Kubernetes resources
at all.

Testing
-------

See `TESTING.rst <TESTING.rst>`_ for the full tiered procedure, and
`EVALUATION.md <EVALUATION.md>`_ for the ``azure`` backend's live-testing
results and what they mean for whether it's a good idea for your deployment.

::

    pip install -e ".[dev]"
    make test

License
-------

This work is licensed under the terms of the `GNU Affero General Public License
(AGPL) <https://www.gnu.org/licenses/agpl-3.0.en.html>`_.

Maintained by
--------------

This Tutor plugin is maintained by Syed Ali Abbas from
`Edly <https://edly.io>`__. Community support is available from the official
`Open edX forum <https://discuss.openedx.org>`__. Do you need help with this
plugin? See the `troubleshooting
<https://docs.tutor.edly.io/troubleshooting.html>`__ section from the Tutor
documentation.
