"""Live S3 conformance checks against a running rustfs backend.

Skipped unless ``OBJSTORAGE_TEST_ENDPOINT`` is set, so ``make test`` stays
offline. These exercise the S3 operations Open edX actually depends on:
signed uploads, presigned download URLs, multipart uploads for large
videos, and anonymous reads from the public bucket. Ported from
tutor-rustfs's own test_s3_conformance.py.

Run against a Tutor deployment with OBJSTORAGE_BACKEND=rustfs::

    export OBJSTORAGE_TEST_ENDPOINT=http://localhost:9000
    export OBJSTORAGE_TEST_ACCESS_KEY="$(tutor config printvalue \
        OPENEDX_AWS_ACCESS_KEY)"
    export OBJSTORAGE_TEST_SECRET_KEY="$(tutor config printvalue \
        OPENEDX_AWS_SECRET_ACCESS_KEY)"
    pip install boto3 requests
    pytest -v tests/test_s3_conformance_rustfs.py
"""

from __future__ import annotations

import os
import typing as t
import uuid

import pytest

ENDPOINT = os.environ.get("OBJSTORAGE_TEST_ENDPOINT")

pytestmark = pytest.mark.skipif(
    not ENDPOINT, reason="set OBJSTORAGE_TEST_ENDPOINT to run live S3 checks"
)

# 6 MiB: above boto3's 5 MiB multipart threshold, so this actually
# exercises the multipart path that video uploads use.
MULTIPART_SIZE = 6 * 1024 * 1024


@pytest.fixture(scope="module")
def s3() -> t.Any:
    boto3 = pytest.importorskip("boto3")
    from botocore.client import Config

    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=os.environ.get("OBJSTORAGE_TEST_ACCESS_KEY", "openedx"),
        aws_secret_access_key=os.environ["OBJSTORAGE_TEST_SECRET_KEY"],
        region_name=os.environ.get("OBJSTORAGE_TEST_REGION", "us-east-1"),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            # Mirrors the AWS_S3_CLIENT_CONFIG set in
            # openedx-common-settings. Without it, boto3 >= 1.36 sends
            # CRC32 checksums that non-AWS S3 servers reject.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


@pytest.fixture(scope="module")
def bucket() -> str:
    return os.environ.get("OBJSTORAGE_TEST_BUCKET", "openedx")


@pytest.fixture(scope="module")
def private_bucket() -> str:
    return os.environ.get("OBJSTORAGE_TEST_PRIVATE_BUCKET", "openedxgrades")


@pytest.fixture
def key(s3: t.Any, bucket: str) -> t.Iterator[str]:
    name = f"tutor-objectstorage-rustfs-test/{uuid.uuid4()}.bin"
    yield name
    try:
        s3.delete_object(Bucket=bucket, Key=name)
    except Exception:  # noqa: BLE001 - cleanup must not mask failures
        pass


def test_list_buckets(s3: t.Any, bucket: str) -> None:
    names = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    assert bucket in names, f"bucket {bucket!r} missing; run `tutor local do init`"


def test_put_and_get_roundtrip(s3: t.Any, bucket: str, key: str) -> None:
    body = b"tutor-objectstorage rustfs conformance"
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == body


def test_head_object_reports_size(s3: t.Any, bucket: str, key: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=b"x" * 128)
    assert s3.head_object(Bucket=bucket, Key=key)["ContentLength"] == 128


def test_multipart_upload(s3: t.Any, bucket: str, key: str) -> None:
    """Video uploads go through the multipart path."""
    import io

    from boto3.s3.transfer import TransferConfig

    payload = b"v" * MULTIPART_SIZE
    s3.upload_fileobj(
        io.BytesIO(payload),
        bucket,
        key,
        Config=TransferConfig(multipart_threshold=5 * 1024 * 1024),
    )
    assert s3.head_object(Bucket=bucket, Key=key)["ContentLength"] == MULTIPART_SIZE


def test_presigned_get_url_is_fetchable(s3: t.Any, bucket: str, key: str) -> None:
    """Grades exports and private downloads are served this way."""
    requests = pytest.importorskip("requests")
    body = b"presigned"
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300
    )
    response = requests.get(url, timeout=30)
    assert response.status_code == 200, response.text[:400]
    assert response.content == body


def test_anonymous_read_on_public_bucket(s3: t.Any, bucket: str, key: str) -> None:
    """The init task marks the common bucket public for forum images."""
    requests = pytest.importorskip("requests")
    s3.put_object(Bucket=bucket, Key=key, Body=b"public")
    response = requests.get(f"{ENDPOINT}/{bucket}/{key}", timeout=30)
    assert response.status_code == 200, (
        f"anonymous read failed ({response.status_code}); "
        "check `rc anonymous set download` ran"
    )


def test_anonymous_write_is_rejected(s3: t.Any, bucket: str) -> None:
    """The public bucket must be readable, not writable.

    `rc anonymous set public` grants anonymous write; `download` does
    not. If this passes with a 2xx, anyone who can reach the endpoint
    can upload to and delete from the bucket without credentials.
    """
    requests = pytest.importorskip("requests")
    probe = f"{ENDPOINT}/{bucket}/tutor-objectstorage-rustfs-test/anon-write-probe.txt"
    response = requests.put(probe, data=b"should be rejected", timeout=30)
    assert response.status_code == 403, (
        f"anonymous write returned {response.status_code}; the bucket policy "
        "grants unauthenticated writes. Use `rc anonymous set download`."
    )
    delete = requests.delete(f"{ENDPOINT}/{bucket}/", timeout=30)
    assert delete.status_code in (403, 405), (
        f"anonymous delete returned {delete.status_code}"
    )


def test_anonymous_read_on_private_bucket_is_rejected(s3: t.Any) -> None:
    private_bucket = os.environ.get("OBJSTORAGE_TEST_PRIVATE_BUCKET", "openedxgrades")
    requests = pytest.importorskip("requests")
    response = requests.get(f"{ENDPOINT}/{private_bucket}/anything", timeout=30)
    assert response.status_code in (403, 404), (
        f"anonymous read of a PRIVATE bucket returned {response.status_code} - "
        "this is a real data leak, not a cosmetic issue"
    )


# --- CORS: regression guard for the Caddy-level fix in patches/rustfs/caddyfile ---
#
# RustFS ships with no CORS headers configured by default (confirmed live:
# a direct OPTIONS preflight against the rustfs container itself, bypassing
# Caddy, returns a bare 200 OK with zero Access-Control-* headers) - unlike
# the `azure` backend, where rclone's `serve s3` sends its own native
# headers and the bug there was duplication, not absence. See
# EVALUATION.md Finding 6/7 for the full history of both.


def test_cors_header_present_and_not_duplicated_on_public_bucket(
    bucket: str, key: str
) -> None:
    requests = pytest.importorskip("requests")
    response = requests.get(
        f"{ENDPOINT}/{bucket}/{key}",
        headers={"Origin": "https://example.com"},
        timeout=30,
    )
    value = response.headers.get("Access-Control-Allow-Origin", "")
    assert value == "*", (
        f"expected exactly one Access-Control-Allow-Origin value, got "
        f"{value!r} - either missing (the original bug) or duplicated "
        f"(the azure backend's distinct bug) would show up here as "
        f"something other than a bare '*'"
    )


def test_cors_preflight_succeeds_on_presigned_private_bucket_url(
    s3: t.Any, private_bucket: str
) -> None:
    """The Instructor Dashboard grade-report download flow this plugin's
    whole CORS fix was found through: a presigned URL for a *private*
    bucket, fetched by a real cross-origin preflighted request - not a
    plain <a> link click, which isn't subject to CORS at all."""
    requests = pytest.importorskip("requests")
    name = f"tutor-objectstorage-rustfs-test/{uuid.uuid4()}.csv"
    s3.put_object(Bucket=private_bucket, Key=name, Body=b"grade,report")
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": private_bucket, "Key": name},
            ExpiresIn=300,
        )
        response = requests.options(
            url,
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-requested-with",
            },
            timeout=30,
        )
        assert response.ok, (
            f"preflight against a presigned private-bucket URL returned "
            f"{response.status_code}, not 2xx - a browser fails the whole "
            f"request before the real GET is ever sent: {response.text[:400]}"
        )
        assert response.headers.get("Access-Control-Allow-Origin"), (
            "preflight response is 2xx but carries no "
            "Access-Control-Allow-Origin header"
        )
    finally:
        try:
            s3.delete_object(Bucket=private_bucket, Key=name)
        except Exception:  # noqa: BLE001 - cleanup must not mask failures
            pass


def test_list_objects_v2(s3: t.Any, bucket: str, key: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=b"listed")
    listing = s3.list_objects_v2(
        Bucket=bucket, Prefix="tutor-objectstorage-rustfs-test/"
    )
    assert key in {o["Key"] for o in listing.get("Contents", [])}


def test_copy_object(s3: t.Any, bucket: str, key: str) -> None:
    """Course import/export copies objects server-side."""
    s3.put_object(Bucket=bucket, Key=key, Body=b"original")
    dest = f"{key}.copy"
    try:
        s3.copy_object(
            Bucket=bucket, Key=dest, CopySource={"Bucket": bucket, "Key": key}
        )
        assert s3.get_object(Bucket=bucket, Key=dest)["Body"].read() == b"original"
    finally:
        s3.delete_object(Bucket=bucket, Key=dest)


def test_delete_object(s3: t.Any, bucket: str, key: str) -> None:
    from botocore.exceptions import ClientError

    s3.put_object(Bucket=bucket, Key=key, Body=b"transient")
    s3.delete_object(Bucket=bucket, Key=key)
    with pytest.raises(ClientError):
        s3.head_object(Bucket=bucket, Key=key)
