from .base import Backend

# -----------------------------------------------------------------------------
# Any external S3-compatible provider: AWS S3, GCS, Ceph RGW, DigitalOcean
# Spaces, or a RustFS/MinIO instance running on a separate machine.
#
# Unlike the rustfs and azure backends, this one runs no local
# infrastructure at all: no compose service, no Kubernetes Deployment, no
# Caddy vhost. It assumes the bucket(s) already exist, created and
# access-controlled out-of-band by the operator - see the README's
# "Using an external S3-compatible provider" section. This mirrors
# tutor-contrib-s3 (https://github.com/cleura/tutor-contrib-s3), the
# independently-maintained plugin this backend reimplements equivalent
# functionality for (see the feature-parity checklist in the project plan).
# -----------------------------------------------------------------------------

BACKEND = Backend(
    name="external_s3",
    defaults={
        # Empty by default so boto3/AWS resolve their own default
        # endpoint; set both for any non-AWS S3-compatible provider.
        # Named ENDPOINT_HOST, not HOST: unlike the other two backends,
        # this is not a Caddy vhost this plugin creates - it is an
        # override of the *provider's own* endpoint.
        "ENDPOINT_HOST": "",
        "PORT": "",
        # Empty by default, deliberately not "us-east-1": a real AWS S3
        # bucket needs a real region, and silently inheriting a
        # meaningless default here would fail at upload time with no
        # helpful error pointing back at this setting.
        "REGION": "",
        "USE_SSL": True,
        "ADDRESSING_STYLE": "auto",
        "CUSTOM_DOMAIN": "",
        "PROFILE_IMAGE_CUSTOM_DOMAIN": "",
        # Defaults to the shared OBJSTORAGE_BUCKET_NAME (see
        # patches/external_s3/openedx-lms-common-settings) but can be
        # set empty to keep profile images on local filesystem, exactly
        # reproducing tutor-contrib-s3's S3_PROFILE_IMAGE_BUCKET behavior.
        "PROFILE_IMAGE_BUCKET": "{{ OBJSTORAGE_BUCKET_NAME }}",
    },
    # Deliberately empty: this backend never self-generates credentials.
    # OPENEDX_AWS_ACCESS_KEY/_SECRET_ACCESS_KEY are left at Tutor-core's
    # own default ("") for this backend - the operator sets them by hand,
    # exactly as with tutor-contrib-s3 today.
    unique={},
)
