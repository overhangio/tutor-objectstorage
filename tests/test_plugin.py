"""Config/hook registration tests.

EXPECTED_KEYS is hand-maintained, deliberately not derived from
backends/*.py or config.py, so that accidentally dropping (or silently
renaming) a key fails loudly here rather than only showing up as a missing
setting at `tutor local launch` time. This mirrors the EXPECTED_KEYS
pattern already proven in tutor-rustfs/tutor-rclone-gateway's own test
suites, generalized over three backends in one namespace instead of one
backend each in its own namespace.
"""

from __future__ import annotations

import typing as t

import pytest
from tutor import exceptions as tutor_exceptions

from tutorobjectstorage import backends, config
from tutorobjectstorage.plugin import check_plugin_conflict, validate_objstorage_config

SHARED_KEYS = {
    "OBJSTORAGE_BACKEND",
    "OBJSTORAGE_VERSION",
    "OBJSTORAGE_BUCKET_NAME",
    "OBJSTORAGE_FILE_UPLOAD_BUCKET_NAME",
    "OBJSTORAGE_VIDEO_UPLOAD_BUCKET_NAME",
    "OBJSTORAGE_GRADES_BUCKET_NAME",
    "OBJSTORAGE_OPENEDX_LEARNING_BUCKET_NAME",
    "OBJSTORAGE_DISCOVERY_BUCKET_NAME",
    "OBJSTORAGE_QUERYSTRING_AUTH",
    # One shared generated secret for all backends, not one per backend -
    # see config.py's CREDENTIAL_OVERRIDES docstring for why.
    "OBJSTORAGE_AWS_SECRET_ACCESS_KEY",
}
RUSTFS_KEYS = {
    "OBJSTORAGE_RUSTFS_HOST",
    "OBJSTORAGE_RUSTFS_CONSOLE_HOST",
    "OBJSTORAGE_RUSTFS_REGION",
    "OBJSTORAGE_RUSTFS_DOCKER_IMAGE",
    "OBJSTORAGE_RUSTFS_RC_DOCKER_IMAGE",
    "OBJSTORAGE_RUSTFS_UID",
    "OBJSTORAGE_RUSTFS_GID",
}
AZURE_KEYS = {
    "OBJSTORAGE_AZURE_HOST",
    "OBJSTORAGE_AZURE_REGION",
    "OBJSTORAGE_AZURE_DOCKER_IMAGE",
    "OBJSTORAGE_AZURE_UID",
    "OBJSTORAGE_AZURE_GID",
    "OBJSTORAGE_AZURE_ACCOUNT",
    "OBJSTORAGE_AZURE_ACCOUNT_KEY",
    "OBJSTORAGE_AZURE_ENDPOINT",
    "OBJSTORAGE_AZURE_AZURITE_DOCKER_IMAGE",
    "OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK",
    "OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT",
}
EXTERNAL_S3_KEYS = {
    "OBJSTORAGE_EXTERNAL_S3_ENDPOINT_HOST",
    "OBJSTORAGE_EXTERNAL_S3_PORT",
    "OBJSTORAGE_EXTERNAL_S3_REGION",
    "OBJSTORAGE_EXTERNAL_S3_USE_SSL",
    "OBJSTORAGE_EXTERNAL_S3_ADDRESSING_STYLE",
    "OBJSTORAGE_EXTERNAL_S3_CUSTOM_DOMAIN",
    "OBJSTORAGE_EXTERNAL_S3_PROFILE_IMAGE_CUSTOM_DOMAIN",
    "OBJSTORAGE_EXTERNAL_S3_PROFILE_IMAGE_BUCKET",
}
EXPECTED_KEYS = SHARED_KEYS | RUSTFS_KEYS | AZURE_KEYS | EXTERNAL_S3_KEYS

# Legacy prefixes from the three plugins this one supersedes/reimplements.
# No bare key under any of these should survive into this plugin's own
# config namespace - that would mean an incomplete rename during porting.
LEGACY_PREFIXES = ("MINIO_", "RUSTFS_", "RCLONEGW_", "S3_")

# tutor-minio's own historical mistake: unprefixed, backend-identifying
# key names leaking into the global config namespace. Carried forward as a
# blocklist from both predecessor plugins' own test suites.
UNPREFIXED_BLOCKLIST = {
    "DOCKER_IMAGE",
    "BUCKET_NAME",
    "REGION",
    "UID",
    "GID",
    "HOST",
    "BACKEND",
}


def test_all_expected_keys_are_defined(config: dict[str, t.Any]) -> None:
    missing = EXPECTED_KEYS - set(config)
    assert not missing, f"missing expected OBJSTORAGE_* keys: {missing}"


def test_no_unexpected_objstorage_keys(config: dict[str, t.Any]) -> None:
    actual = {k for k in config if k.startswith("OBJSTORAGE_")}
    unexpected = actual - EXPECTED_KEYS
    assert not unexpected, f"unexpected OBJSTORAGE_* keys: {unexpected}"


def test_no_legacy_prefixed_keys_leak(config: dict[str, t.Any]) -> None:
    for prefix in LEGACY_PREFIXES:
        leaked = {k for k in config if k.startswith(prefix)}
        assert not leaked, f"legacy-prefixed keys leaked into config: {leaked}"


def test_plugin_config_is_namespaced(config: dict[str, t.Any]) -> None:
    unprefixed_present = UNPREFIXED_BLOCKLIST & set(config)
    assert not unprefixed_present, (
        f"unprefixed keys leaked into the global namespace: {unprefixed_present} "
        "(see tutor-minio's own historical MC_DOCKER_IMAGE mistake)"
    )


def test_default_backend_is_rustfs(config: dict[str, t.Any]) -> None:
    assert config["OBJSTORAGE_BACKEND"] == "rustfs"


def test_azure_has_no_console_host_setting(config: dict[str, t.Any]) -> None:
    # rclone has no web console or bucket browser, unlike RustFS.
    assert "OBJSTORAGE_AZURE_CONSOLE_HOST" not in config


def test_docker_images_are_pinned(config: dict[str, t.Any]) -> None:
    assert config["OBJSTORAGE_RUSTFS_DOCKER_IMAGE"] != "latest"
    assert config["OBJSTORAGE_AZURE_DOCKER_IMAGE"] == "docker.io/rclone/rclone:1.75.1"


def test_hosts_derive_from_lms_host(config: dict[str, t.Any]) -> None:
    assert config["OBJSTORAGE_RUSTFS_HOST"] == f"files.{config['LMS_HOST']}"


@pytest.mark.parametrize(
    "backend,should_generate_secret",
    [("rustfs", True), ("azure", True), ("external_s3", False)],
)
def test_openedx_credentials_are_wired(
    config_by_backend: dict[str, dict[str, t.Any]],
    backend: str,
    should_generate_secret: bool,
) -> None:
    # The credential override is deliberately unconditional (see config.py's
    # CREDENTIAL_OVERRIDES docstring - a backend-conditional version breaks
    # `tutor config save` outright), so OPENEDX_AWS_ACCESS_KEY/_SECRET are
    # always "openedx"/the shared generated secret regardless of backend,
    # including external_s3 - which is why `should_generate_secret` is
    # unused here now; it's kept as a parametrize axis as documentation of
    # intent, and this test asserts the actual (uniform) behavior.
    del should_generate_secret
    cfg = config_by_backend[backend]
    assert cfg["OPENEDX_AWS_ACCESS_KEY"] == "openedx"
    assert len(cfg["OPENEDX_AWS_SECRET_ACCESS_KEY"]) == 24
    assert (
        cfg["OPENEDX_AWS_SECRET_ACCESS_KEY"] == cfg["OBJSTORAGE_AWS_SECRET_ACCESS_KEY"]
    )


def test_init_task_registered_once_per_backend() -> None:
    from tutor import hooks as tutor_hooks

    services = [
        service for service, _ in tutor_hooks.Filters.CLI_DO_INIT_TASKS.iterate()
    ]
    matching = [s for s in services if s == config.INIT_SERVICE_NAME]
    # One registration per backend (rustfs.sh, azure.sh, external_s3.sh).
    assert len(matching) == len(backends.ALL)


@pytest.mark.parametrize("plugin_name", ["minio", "rustfs", "rclonegateway", "s3"])
def test_refuses_to_run_alongside_legacy_plugins(plugin_name: str) -> None:
    with pytest.raises(tutor_exceptions.TutorError):
        check_plugin_conflict(plugin_name)


@pytest.mark.parametrize("plugin_name", ["discovery", "xqueue", "mfe", "objectstorage"])
def test_other_plugins_do_not_trigger_conflict(plugin_name: str) -> None:
    check_plugin_conflict(plugin_name)  # must not raise


def test_invalid_backend_value_is_rejected() -> None:
    with pytest.raises(tutor_exceptions.TutorError, match="OBJSTORAGE_BACKEND"):
        validate_objstorage_config({"OBJSTORAGE_BACKEND": "not-a-real-backend"})


def test_azure_without_acknowledgement_is_rejected() -> None:
    with pytest.raises(
        tutor_exceptions.TutorError, match="OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK"
    ):
        validate_objstorage_config(
            {"OBJSTORAGE_BACKEND": "azure", "OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK": False}
        )


def test_azure_with_acknowledgement_is_accepted() -> None:
    validate_objstorage_config(
        {"OBJSTORAGE_BACKEND": "azure", "OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK": True}
    )  # must not raise


@pytest.mark.parametrize("backend", ["rustfs", "external_s3"])
def test_backends_without_a_risk_gate_need_no_acknowledgement(backend: str) -> None:
    validate_objstorage_config({"OBJSTORAGE_BACKEND": backend})  # must not raise


def test_tutor_dependency_does_not_exclude_current_tutor_releases() -> None:
    """Regression test: `pip install tutor-objectstorage` must not downgrade
    an existing Tutor install.

    This plugin once declared ``tutor>=21.0.0,<22.0.0`` - a stale upper
    bound nothing in this codebase actually requires (confirmed live: every
    command used throughout TESTING.rst's Tiers 2-4 runs unmodified against
    Tutor 22.0.2). Installing the plugin into any Tutor 22.x environment -
    the version every other current Tutor plugin on PyPI depends on -
    silently downgraded Tutor to the newest 21.x release instead, which
    then breaks every other installed plugin's own `tutor<23.0.0,>=22.0.0`
    constraint. `pip` reports this as a dependency conflict warning, not an
    install failure, so it is easy to miss.

    Importlib.metadata reads the dependency specifier Tutor itself (via
    `tutor plugins list`) will see - the same place an accidental
    re-narrowing of this bound would actually bite.
    """
    from importlib.metadata import requires

    from packaging.requirements import Requirement
    from packaging.version import Version

    dependencies = requires("tutor-objectstorage") or []
    tutor_requirement = next(
        Requirement(dep) for dep in dependencies if Requirement(dep).name == "tutor"
    )
    assert tutor_requirement.specifier.contains(Version("22.0.2"), prereleases=True), (
        f"tutor-objectstorage's own 'tutor' dependency ({tutor_requirement.specifier}) "
        "excludes tutor 22.0.2, the version every other current Tutor plugin "
        "requires - this would silently downgrade Tutor on install"
    )
    assert tutor_requirement.specifier.contains(Version("20.0.5"), prereleases=True), (
        f"tutor-objectstorage's own 'tutor' dependency ({tutor_requirement.specifier}) "
        "excludes tutor 20.0.5 (Teak) - this plugin is meant to support Teak too"
    )
