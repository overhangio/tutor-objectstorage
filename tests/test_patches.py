"""Template rendering tests, generalized over three backends.

Shared invariants (render cleanly, parse as valid YAML/Python, use only
real Tutor patch hooks) are parametrized over every backend. The
cross-backend containment tests are the one genuinely new class this
merge's modular-backend design needs that neither predecessor plugin's
suite could express on its own: selecting backend X must never leak
backend Y's infrastructure into the rendered output.
"""

from __future__ import annotations

import importlib
import re
import sys
import types
import typing as t

import pytest
import yaml
from tutor import env as tutor_env

from tests.conftest import BACKENDS, patch_names, read_patch, read_task_script

YAML_PATCHES = {
    "k8s-deployments",
    "k8s-jobs",
    "k8s-override",
    "k8s-services",
    "k8s-volumes",
    "local-docker-compose-caddy-aliases",
    "local-docker-compose-cms-dependencies",
    "local-docker-compose-dev-services",
    "local-docker-compose-jobs-services",
    "local-docker-compose-lms-dependencies",
    "local-docker-compose-services",
    "openedx-auth",
}
PYTHON_PATCHES = {
    "discovery-common-settings",
    "discovery-development-settings",
    "openedx-cms-common-settings",
    "openedx-common-settings",
    "openedx-development-settings",
    "openedx-lms-common-settings",
    "xqueue-settings",
}
CADDYFILE_PATCHES = {"caddyfile"}

# Patches where a given backend legitimately contributes nothing (the
# patch slot renders empty/whitespace for that backend). Used to relax the
# YAML/Python parse checks: "renders to nothing" is a pass, not a failure,
# for exactly these (backend, patch) pairs.
EXPECTED_EMPTY = {
    ("external_s3", "k8s-deployments"),
    ("external_s3", "k8s-services"),
    ("external_s3", "k8s-volumes"),
    ("external_s3", "k8s-override"),
    ("external_s3", "local-docker-compose-services"),
    ("external_s3", "local-docker-compose-dev-services"),
    ("external_s3", "local-docker-compose-caddy-aliases"),
    ("external_s3", "local-docker-compose-lms-dependencies"),
    ("external_s3", "local-docker-compose-cms-dependencies"),
    ("external_s3", "openedx-development-settings"),
    ("external_s3", "discovery-development-settings"),
    ("external_s3", "caddyfile"),
    ("rustfs", "k8s-override"),
    ("azure", "k8s-volumes"),
}


def test_all_patches_are_categorised() -> None:
    categorised = YAML_PATCHES | PYTHON_PATCHES | CADDYFILE_PATCHES
    uncategorised = set(patch_names()) - categorised
    assert not uncategorised, f"add these to a category in this file: {uncategorised}"


@pytest.mark.parametrize("backend", BACKENDS)
def test_patch_renders_without_error(
    backend: str, renderer_by_backend: dict[str, tutor_env.Renderer]
) -> None:
    renderer = renderer_by_backend[backend]
    for name in patch_names():
        renderer.patch(name)  # must not raise


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_unrendered_jinja(
    backend: str, rendered_by_backend: dict[str, dict[str, str]]
) -> None:
    for name, text in rendered_by_backend[backend].items():
        assert "{{" not in text, f"{backend}/{name}: unrendered {{{{ }}}} left"
        assert "{%" not in text, f"{backend}/{name}: unrendered {{% %}} left"


@pytest.mark.parametrize("backend", BACKENDS)
def test_yaml_patches_parse(
    backend: str, rendered_by_backend: dict[str, dict[str, str]]
) -> None:
    for name in YAML_PATCHES:
        text = rendered_by_backend[backend][name]
        if (backend, name) in EXPECTED_EMPTY:
            assert text.strip() == "", (
                f"{backend}/{name}: expected empty, got: {text!r}"
            )
            continue
        parsed = list(yaml.safe_load_all(text))
        assert parsed and parsed[0] is not None, f"{backend}/{name} did not parse"


@pytest.mark.parametrize("backend", BACKENDS)
def test_python_patches_are_syntactically_valid(
    backend: str, rendered_by_backend: dict[str, dict[str, str]]
) -> None:
    for name in PYTHON_PATCHES:
        text = rendered_by_backend[backend][name]
        if (backend, name) in EXPECTED_EMPTY:
            assert text.strip() == "", (
                f"{backend}/{name}: expected empty, got: {text!r}"
            )
            continue
        compile(text, name, "exec")  # must not raise


# Renders the composite settings files (not just the isolated patch) so a
# patch registered under the wrong Tutor hook name shows up here too.
# Checks STORAGES["profile_image"], not PROFILE_IMAGE_BACKEND:
# get_profile_image_storage() prefers the former when present, and
# S3Storage rejects PROFILE_IMAGE_BACKEND's base_url kwarg (confirmed
# live - lms/urls.py needs base_url on PROFILE_IMAGE_BACKEND itself for
# its dev-mode static() route, so that setting must stay untouched).
@pytest.mark.parametrize("backend", BACKENDS)
def test_profile_image_s3_override_applies_in_both_dev_and_production(
    backend: str, config_by_backend: dict[str, t.Any]
) -> None:
    cfg = config_by_backend[backend]
    bucket_name = cfg["OBJSTORAGE_BUCKET_NAME"]
    for settings_file in ("development.py", "production.py"):
        rendered = tutor_env.render_file(
            cfg, "apps", "openedx", "settings", "lms", settings_file
        )
        assert isinstance(rendered, str)
        start = rendered.rfind('STORAGES["profile_image"] = {')
        assert start != -1, (
            f"{backend}/lms/{settings_file}: the S3 STORAGES['profile_image'] "
            "override never rendered - profile images would stay on local "
            "disk under this settings file"
        )
        # Brace-depth scan, not a literal "\n}\n" search: the dict literal is
        # now nested inside an `if "STORAGES" in globals():` branch (Teak
        # compat), so its closing brace is indented and would otherwise be
        # skipped in favor of some unrelated later top-level "}" - silently
        # swallowing the Teak else-branch's unrelated code into "block".
        open_brace = rendered.index("{", start)
        depth = 0
        end = open_brace
        for end, ch in enumerate(rendered[open_brace:], start=open_brace):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        block = rendered[start : end + 1]
        assert f'"bucket_name": "{bucket_name}"' in block, (
            f"{backend}/lms/{settings_file}: STORAGES['profile_image'] did "
            "not pick up the configured bucket name"
        )
        assert '"base_url"' not in block, (
            f"{backend}/lms/{settings_file}: STORAGES['profile_image'] "
            "options must not include base_url - S3Storage rejects it as "
            "an invalid kwarg"
        )


# --- Teak (Tutor 20.x) backward compatibility: exec, not just compile ------
# test_python_patches_are_syntactically_valid only compile()s - it would
# pass on code that raises NameError/ImproperlyConfigured the instant it
# actually runs, exactly the bug these patches fix (confirmed live against a
# real Tutor 20.0.5 + Teak edx-platform: `NameError: name 'STORAGES' is not
# defined`). These exec() the real rendered patches against two fabricated
# namespaces standing in for each era's pre-existing settings-module state.
# This is the durable regression guard: CI always resolves the newest Tutor
# satisfying pyproject.toml's range, so it will never actually install Tutor
# 20.x itself even after widening that range.


class _FakeS3Boto3Storage:
    """Stand-in for storages.backends.s3boto3.S3Boto3Storage: django-storages
    is a dependency of the edx-platform venv these patches actually execute
    inside, not of this plugin's own test environment."""

    def __init__(self, **kwargs: t.Any) -> None:
        self.kwargs = kwargs


class _FakeBotoConfig:
    """Stand-in for botocore.client.Config: botocore/boto3 are dependencies
    of the edx-platform venv these patches actually execute inside, not of
    this plugin's own test environment (confirmed: whether `import botocore`
    succeeds here depends on what happens to be installed transitively in
    whichever venv runs these tests, so it must be stubbed rather than relied
    on)."""

    def __init__(self, **kwargs: t.Any) -> None:
        self.kwargs = kwargs


def _install_fake_s3_runtime_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    s3boto3_mod = types.ModuleType("storages.backends.s3boto3")
    s3boto3_mod.S3Boto3Storage = _FakeS3Boto3Storage  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "storages", types.ModuleType("storages"))
    monkeypatch.setitem(
        sys.modules, "storages.backends", types.ModuleType("storages.backends")
    )
    monkeypatch.setitem(sys.modules, "storages.backends.s3boto3", s3boto3_mod)

    client_mod = types.ModuleType("botocore.client")
    client_mod.Config = _FakeBotoConfig  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(sys.modules, "botocore.client", client_mod)


def _base_namespace() -> dict[str, t.Any]:
    return {
        "__name__": "lms.envs.tutor.development",
        "VIDEO_IMAGE_SETTINGS": {"STORAGE_KWARGS": {"location": "/video-images/"}},
        "VIDEO_TRANSCRIPTS_SETTINGS": {
            "STORAGE_KWARGS": {"location": "/video-transcripts/"}
        },
        "GRADES_DOWNLOAD": {"STORAGE_KWARGS": {"location": "/grades/"}},
    }


def _teak_namespace() -> dict[str, t.Any]:
    """No STORAGES, no OPENEDX_LEARNING - confirmed absent from Teak's real
    edx-platform source even though it runs Django 4.2.20. PROFILE_IMAGE_
    BACKEND is in the old shape get_profile_image_storage() reads directly,
    base_url included (lms/urls.py's dev-mode static() route needs it)."""
    ns = _base_namespace()
    ns["PROFILE_IMAGE_BACKEND"] = {
        "class": "storages.backends.s3boto3.S3Boto3Storage",
        "options": {
            "location": "profile-images/",
            "base_url": "/media/profile-images/",
        },
    }
    return ns


def _verawood_namespace() -> dict[str, t.Any]:
    """STORAGES/OPENEDX_LEARNING pre-populated, matching Verawood/Ulmo's
    real edx-platform settings-module state."""
    ns = _base_namespace()
    ns["STORAGES"] = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}
    }
    ns["OPENEDX_LEARNING"] = {"MEDIA": {}}
    ns["PROFILE_IMAGE_BACKEND"] = {
        "class": "openedx.core.storage.OverwriteStorage",
        "options": {"location": "profile-images/"},
    }
    return ns


@pytest.mark.parametrize("backend", BACKENDS)
def test_shared_settings_exec_cleanly_on_teak(
    backend: str,
    rendered_by_backend: dict[str, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_s3_runtime_modules(monkeypatch)
    ns = _teak_namespace()
    exec(rendered_by_backend[backend]["openedx-common-settings"], ns)  # must not raise

    assert "STORAGES" not in ns
    assert ns["DEFAULT_FILE_STORAGE"] == "storages.backends.s3boto3.S3Boto3Storage"
    assert "OPENEDX_LEARNING" not in ns, (
        "OPENEDX_LEARNING doesn't exist in Teak's edx-platform at all - "
        "the guard must not create it"
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_shared_settings_exec_cleanly_on_verawood(
    backend: str,
    rendered_by_backend: dict[str, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_s3_runtime_modules(monkeypatch)
    ns = _verawood_namespace()
    exec(rendered_by_backend[backend]["openedx-common-settings"], ns)  # must not raise

    assert "DEFAULT_FILE_STORAGE" not in ns
    assert (
        ns["STORAGES"]["default"]["BACKEND"]
        == "storages.backends.s3boto3.S3Boto3Storage"
    )
    assert (
        ns["OPENEDX_LEARNING"]["MEDIA"]["BACKEND"]
        == "storages.backends.s3boto3.S3Boto3Storage"
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_lms_profile_image_exec_cleanly_on_teak(
    backend: str,
    rendered_by_backend: dict[str, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_s3_runtime_modules(monkeypatch)
    ns = _teak_namespace()
    exec(rendered_by_backend[backend]["openedx-common-settings"], ns)
    exec(
        rendered_by_backend[backend]["openedx-lms-common-settings"], ns
    )  # must not raise

    assert "STORAGES" not in ns
    backend_cls = ns["PROFILE_IMAGE_BACKEND"]["class"]
    assert (
        backend_cls == "lms.envs.tutor.development._ObjstorageProfileImageS3Storage"
    ), (
        "the wrapper class must be referenced via a self-referential "
        "__name__-based dotted path, not a hardcoded module"
    )
    options = ns["PROFILE_IMAGE_BACKEND"]["options"]
    assert "base_url" in options, (
        "lms/urls.py's dev-mode static() route reads "
        "PROFILE_IMAGE_BACKEND['options']['base_url'] directly - it must stay"
    )

    # The wrapper itself must actually strip base_url before delegating,
    # confirmed live against the real Teak image - S3Storage otherwise
    # rejects it as an unrecognized constructor kwarg.
    wrapper_cls = ns["_ObjstorageProfileImageS3Storage"]
    instance = wrapper_cls(**options)
    assert "base_url" not in instance.kwargs
    assert instance.kwargs["location"] == options["location"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_lms_profile_image_exec_cleanly_on_verawood(
    backend: str,
    rendered_by_backend: dict[str, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_s3_runtime_modules(monkeypatch)
    ns = _verawood_namespace()
    exec(rendered_by_backend[backend]["openedx-common-settings"], ns)
    exec(
        rendered_by_backend[backend]["openedx-lms-common-settings"], ns
    )  # must not raise

    assert ns["STORAGES"]["profile_image"]["OPTIONS"]["location"] == "profile-images/"
    assert "_ObjstorageProfileImageS3Storage" not in ns
    assert (
        ns["PROFILE_IMAGE_BACKEND"]["class"] == "openedx.core.storage.OverwriteStorage"
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_discovery_settings_exec_cleanly_on_teak(
    backend: str,
    rendered_by_backend: dict[str, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_s3_runtime_modules(monkeypatch)
    ns: dict[str, t.Any] = {}
    # must not raise
    exec(rendered_by_backend[backend]["discovery-common-settings"], ns)

    assert "STORAGES" not in ns
    assert ns["DEFAULT_FILE_STORAGE"] == "storages.backends.s3boto3.S3Boto3Storage"


@pytest.mark.parametrize("backend", BACKENDS)
def test_discovery_settings_exec_cleanly_on_verawood(
    backend: str,
    rendered_by_backend: dict[str, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_s3_runtime_modules(monkeypatch)
    ns: dict[str, t.Any] = {
        "STORAGES": {
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}
        }
    }
    # must not raise
    exec(rendered_by_backend[backend]["discovery-common-settings"], ns)

    assert "DEFAULT_FILE_STORAGE" not in ns
    assert (
        ns["STORAGES"]["default"]["BACKEND"]
        == "storages.backends.s3boto3.S3Boto3Storage"
    )


def test_caddyfile_is_empty_for_external_s3(
    rendered_by_backend: dict[str, dict[str, str]],
) -> None:
    assert rendered_by_backend["external_s3"]["caddyfile"].strip() == ""


@pytest.mark.parametrize("backend", ["rustfs", "azure"])
def test_caddyfile_is_nonempty_for_backends_with_a_vhost(
    backend: str, rendered_by_backend: dict[str, dict[str, str]]
) -> None:
    assert rendered_by_backend[backend]["caddyfile"].strip() != ""


# --- cross-backend containment ---------------------------------------------
# The direct regression guard for the single biggest risk this merge's
# self-guarded-fragment-per-backend-directory design introduces: a
# forgotten `{% if OBJSTORAGE_BACKEND == "..." %}` guard leaking one
# backend's resources into another backend's rendered output.
BACKEND_MARKERS: dict[str, list[str]] = {
    "rustfs": ["rustfs:", "rc mb", "rc anonymous", "/rustfs/console/"],
    "azure": ["rclonegw", "azureblob:", "RCLONE_CONFIG_AZUREBLOB", "rclone mkdir"],
    "external_s3": [],
}
INFRA_PATCH_NAMES = [
    "caddyfile",
    "k8s-deployments",
    "k8s-services",
    "k8s-override",
    "k8s-volumes",
    "local-docker-compose-services",
    "local-docker-compose-jobs-services",
    "k8s-jobs",
]


@pytest.mark.parametrize("backend", BACKENDS)
def test_only_selected_backend_renders_into_infra_patches(
    backend: str, rendered_by_backend: dict[str, dict[str, str]]
) -> None:
    rendered = rendered_by_backend[backend]
    for other, markers in BACKEND_MARKERS.items():
        if other == backend:
            continue
        for marker in markers:
            for patch_name in INFRA_PATCH_NAMES:
                text = rendered[patch_name]
                assert marker not in text, (
                    f"backend={backend!r} rendered {other!r}'s marker "
                    f"{marker!r} into {patch_name} - a missing "
                    f"{{% if OBJSTORAGE_BACKEND == ... %}} guard"
                )


@pytest.mark.parametrize("backend", BACKENDS)
def test_only_selected_backend_init_script_has_content(
    backend: str, renderer_by_backend: dict[str, tutor_env.Renderer]
) -> None:
    renderer = renderer_by_backend[backend]
    for other in BACKENDS:
        if other == backend:
            continue
        other_script = renderer.render_str(read_task_script(other))
        assert other_script.strip() == "", (
            f"backend={backend!r} still rendered {other!r}'s init script "
            f"content: {other_script!r}"
        )


# --- the Azure risk gate's Jinja-level half ---------------------------------
# The CONFIG_LOADED hard-fail (tests/test_plugin.py) is the primary gate,
# but offline template-rendering (what this whole file does) bypasses
# CONFIG_LOADED entirely - only the Jinja-level
# `and OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK` guard on every Azure patch makes
# "no Azure infra renders without the flag" observable here.
def test_azure_infra_is_empty_without_risk_acknowledgement() -> None:
    from tests.conftest import render_bypassing_config_loaded

    # Deliberately bypasses CONFIG_LOADED (see the helper's docstring):
    # going through the real load_full() pipeline here would correctly
    # hard-fail before we ever got to inspect the Jinja output - which is
    # exactly what tests/test_plugin.py's
    # test_azure_without_acknowledgement_is_rejected already covers. This
    # test exists to prove the second, independent layer underneath that:
    # the patches themselves withhold Azure infrastructure even if
    # something else's validation were ever bypassed.
    cfg = render_bypassing_config_loaded(
        OBJSTORAGE_BACKEND="azure", OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK=False
    )
    renderer = tutor_env.Renderer(cfg)
    for patch_name in INFRA_PATCH_NAMES:
        text = renderer.patch(patch_name).strip()
        assert text == "", (
            f"{patch_name} rendered despite risk flag being false: {text!r}"
        )
    azure_script = renderer.render_str(read_task_script("azure")).strip()
    assert azure_script == "", (
        f"azure init script rendered despite risk flag being false: {azure_script!r}"
    )


# --- the Caddy discovery-bucket guard ---------------------------------------
# Regression guard for a real bug caught by adversarial design review
# before shipping in tutor-rclone-gateway: an *unguarded* second path token
# in the @public_read matcher collapses it to a bare "/*", routing every
# private bucket to the anonymous backend the moment OBJSTORAGE_DISCOVERY_
# BUCKET_NAME is non-empty. Tested by overriding the key directly rather
# than deriving it from PLUGINS, so this isolates the Caddyfile's own
# guard logic from whatever derives the key's value in a real deployment.
def test_caddyfile_discovery_clause_is_safe_when_disabled(
    base_config: dict[str, t.Any],
) -> None:
    from tests.conftest import render_for_backend

    cfg = render_for_backend(base_config, "azure", OBJSTORAGE_DISCOVERY_BUCKET_NAME="")
    caddy = tutor_env.Renderer(cfg).patch("caddyfile")
    match = re.search(r"^\s*path\s+(.*)$", caddy, re.MULTILINE)
    assert match is not None
    path_line = match.group(1).strip()
    assert path_line == f"/{cfg['OBJSTORAGE_BUCKET_NAME']}/*"


def test_caddyfile_discovery_clause_is_present_when_enabled(
    base_config: dict[str, t.Any],
) -> None:
    from tests.conftest import render_for_backend

    cfg = render_for_backend(
        base_config, "azure", OBJSTORAGE_DISCOVERY_BUCKET_NAME="discoveryuploads"
    )
    caddy = tutor_env.Renderer(cfg).patch("caddyfile")
    assert "/discoveryuploads/*" in caddy
    assert f"/{cfg['OBJSTORAGE_BUCKET_NAME']}/*" in caddy


# --- rclone's lack of a bucket-policy step -----------------------------------
def test_azure_init_script_has_no_bucket_policy_step() -> None:
    script = read_task_script("azure")
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("{%"):
            continue
        lowered = stripped.lower()
        for forbidden in ("policy", "anonymous", "acl"):
            assert forbidden not in lowered, f"unexpected {forbidden!r} in: {line!r}"


# --- the azure backend must not double up rclone's own CORS headers, and
# --- must answer OPTIONS itself -----------------------------------------
# Confirmed live, through the real Caddy + rclonegw stack: rclone's `serve
# s3` already emits its own complete Access-Control-* headers on a real
# GET/HEAD/PUT/etc. response. The *previous* version of this Caddyfile
# unconditionally added its own `header Access-Control-Allow-Origin *` on
# top, producing the header *twice* on the actual response - a real
# browser rejects that exactly like a missing header ("contains multiple
# values '*, *', but only one is allowed"). Separately, `rclonegw`'s own
# OPTIONS handling is never preflight-safe: against a presigned URL's
# exact query string (what a real browser preflight actually requests)
# it validates the signature regardless of method and answers a bare
# `403 Forbidden`, since a presigned URL's signature is computed for the
# method it was minted for (GET), never OPTIONS - and a preflight
# response must be 2xx or the browser never sends the real request. Live
# conformance regressions: test_cors_allow_origin_header_is_not_duplicated
# and test_cors_preflight_succeeds_on_presigned_private_bucket_url in
# test_s3_conformance_azure.py. This is the fast, offline guard for the
# same fix at the template level.
def test_azure_caddyfile_does_not_duplicate_cors_headers_and_handles_options(
    base_config: dict[str, t.Any],
) -> None:
    from tests.conftest import render_for_backend

    cfg = render_for_backend(base_config, "azure")
    caddy = tutor_env.Renderer(cfg).patch("caddyfile")

    assert caddy.count("header Access-Control-Allow-Origin") <= 1, (
        "rclone's `serve s3` already sends its own Access-Control-Allow-Origin "
        "on a real GET - adding a second one here duplicates it on the "
        "response, which a browser rejects just like a missing header"
    )

    # A bare count doesn't confirm *where* the header lives: a future edit
    # could move it out of the OPTIONS-only @cors_preflight block and into
    # @public_read or the default handle{} block - both of which proxy
    # real GET/HEAD/PUT traffic to rclone - and still pass the count check
    # above (still exactly one occurrence), the OPTIONS regex below
    # (untouched, still present in @cors_preflight) and the respond-204
    # regex (ditto), even though that reintroduces the exact live
    # duplicate-header bug this test exists to catch. Pin the header to
    # the preflight-only block, and assert it is absent everywhere else in
    # the file, so a misplaced header fails here instead of only showing
    # up in the live conformance test.
    preflight_match = re.search(
        r"handle @cors_preflight \{(.*?)\n    \}", caddy, re.DOTALL
    )
    assert preflight_match is not None, (
        "expected an OPTIONS-only `handle @cors_preflight` block"
    )
    assert "header Access-Control-Allow-Origin" in preflight_match.group(1), (
        "the Access-Control-Allow-Origin header must be set inside the "
        "OPTIONS-only preflight block, not on a path that also serves "
        "real GET/HEAD/PUT responses"
    )
    rest_of_caddyfile = (
        caddy[: preflight_match.start()] + caddy[preflight_match.end() :]
    )
    assert "header Access-Control-Allow-Origin" not in rest_of_caddyfile, (
        "found outside the preflight block - rclone's `serve s3` already "
        "sends its own Access-Control-Allow-Origin on a real GET/HEAD/PUT "
        "response proxied through @public_read or the default handle{} "
        "block, so adding a second one there duplicates it exactly like "
        "the live bug this test guards against"
    )

    assert re.search(r"method\s+OPTIONS", caddy), (
        "OPTIONS must be matched and answered directly at Caddy - "
        "rclonegw's own OPTIONS handling validates a presigned URL's "
        "signature against the wrong method and returns a bare 403, "
        "which fails a real preflight outright"
    )
    assert re.search(r"respond\s+204", caddy), (
        "the OPTIONS short-circuit must itself answer with a clean 2xx "
        "status - a preflight fails if its response is not 2xx, "
        "regardless of which headers ride along with it"
    )


def test_rustfs_init_script_never_sets_anonymous_public() -> None:
    script = read_task_script("rustfs")
    assert "anonymous set public" not in script
    assert "anonymous set download" in script


# --- the console-login redirect must not intercept signed requests ----------
# RustFS's own console frontend talks straight to the bare root for its
# signed STS/S3 calls (POST / for the login handshake, GET
# /?x-id=ListBuckets for the dashboard) - confirmed live against a real
# running stack. An earlier version of this patch redirected literally
# every request to "/" (signed or not) to /rustfs/console/, which broke
# those signed calls (the browser follows the redirect still carrying the
# original signature, which no longer matches the path RustFS actually
# receives) and surfaced as `SignatureDoesNotMatch: missing signed header:
# content-type` instead of a login failure - not a credentials problem,
# a routing one.
def test_rustfs_console_redirect_does_not_intercept_signed_requests(
    base_config: dict[str, t.Any],
) -> None:
    from tests.conftest import render_for_backend

    cfg = render_for_backend(base_config, "rustfs")
    caddy = tutor_env.Renderer(cfg).patch("caddyfile")

    assert "redir / /rustfs/console/" not in caddy, (
        "an unconditional `redir /` also redirects the console's own "
        "signed STS/S3 calls to the bare root, breaking login"
    )
    assert re.search(r"redir\s+@\w+\s+/rustfs/console/\s+302", caddy), (
        "expected the redirect to use a named matcher, not a bare path"
    )
    assert "not header Authorization *" in caddy, (
        "the matcher must exclude header-signed requests (e.g. the "
        "console's own STS login POST /)"
    )
    assert "not query X-Amz-Signature=*" in caddy, (
        "the matcher must exclude presigned/query-signed requests (e.g. "
        "the console's own GET /?x-id=ListBuckets)"
    )


# --- RustFS does not send CORS headers by default ---------------------------
# Confirmed live, through the real Caddy-fronted stack: a direct OPTIONS
# preflight against the rustfs container (bypassing Caddy) returns a bare
# `200 OK` with zero Access-Control-* headers, and a real cross-origin
# browser fetch against a presigned grade-report URL - the Data
# Download/Instructor-dashboard download flow - was blocked with "No
# 'Access-Control-Allow-Origin' header is present on the requested
# resource." This contradicts EVALUATION.md's original comparison table
# ("CORS | Requires proxy-layer headers | Native default" for azure vs.
# rustfs): RustFS can do CORS (`rc bucket cors`), but does not do it by
# default, the same as a fresh AWS S3 bucket. Fixed the same way the
# `azure` backend already is: headers injected at the proxy. This is the
# regression guard for that fix, scoped to the S3 API site block only (not
# the console block, which browsers never fetch cross-origin).
def test_rustfs_caddyfile_sets_cors_headers_on_s3_api(
    base_config: dict[str, t.Any],
) -> None:
    from tests.conftest import render_for_backend

    cfg = render_for_backend(base_config, "rustfs")
    caddy = tutor_env.Renderer(cfg).patch("caddyfile")

    s3_api_block = caddy.split("# RustFS web console.")[0]

    assert "Access-Control-Allow-Origin *" in s3_api_block, (
        "no cross-origin fetch/XHR against a presigned rustfs URL (e.g. "
        "the Instructor Dashboard's grade-report download) will ever "
        "succeed without this - confirmed live, RustFS does not send it "
        "by default"
    )
    assert re.search(r"Access-Control-Allow-Methods\s+[\"']?.*GET", s3_api_block), (
        "needed for a *preflighted* cross-origin request (one with "
        "custom headers) to pass - confirmed live, a direct OPTIONS "
        "preflight against the rustfs container returns 200 with no "
        "Access-Control-* headers at all"
    )
    assert "Access-Control-Allow-Headers" in s3_api_block


# --- hook-name validity ------------------------------------------------------
# Confirmed load-bearing by tutor-rclone-gateway's own development history:
# this is the test that would have caught `k8s-networkpolicy` (a
# non-existent hook) vs. the correct `k8s-override`. Dynamically greps
# installed tutor/tutordiscovery/tutorxqueue packages for literal
# `patch("...")` calls rather than hand-maintaining a list of valid hook
# names, so it stays correct as Tutor core evolves.
def _patch_hooks_called_in(package_name: str) -> set[str]:
    try:
        package = importlib.import_module(package_name)
    except ImportError:
        return set()
    import os

    hooks: set[str] = set()
    root = os.path.dirname(package.__file__ or "")
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            hooks.update(re.findall(r'patch\("([^"]+)"\)', text))
    return hooks


def test_patch_names_are_real_tutor_hooks() -> None:
    core_hooks = _patch_hooks_called_in("tutor")
    assert core_hooks, "found no patch() calls in Tutor's own templates at all"
    sibling_hooks = _patch_hooks_called_in("tutordiscovery") | _patch_hooks_called_in(
        "tutorxqueue"
    )
    checkable = set(patch_names())
    if not sibling_hooks:
        checkable = {
            name
            for name in checkable
            if not (name.startswith("discovery-") or name.startswith("xqueue-"))
        }
    unknown = checkable - core_hooks - sibling_hooks
    assert not unknown, (
        f"these patch filenames don't match any real Tutor patch(...) call: {unknown}"
    )


def test_read_patch_resolves_every_name() -> None:
    for name in patch_names():
        read_patch(name)  # must not raise
