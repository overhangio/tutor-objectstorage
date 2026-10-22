# Changelog

<!-- scriv-insert-here -->

## Unreleased

- [Feature] Support Tutor 20.x (Teak), in addition to the already-supported
  21.x/22.x (Ulmo/Verawood). Teak's edx-platform and course-discovery still
  use Django's old `DEFAULT_FILE_STORAGE` string setting instead of the
  `STORAGES` dict, so every settings patch that touches `STORAGES` now
  branches on `"STORAGES" in globals()` rather than the Django version
  (Teak itself runs Django 4.2.20 too, so a version check would get this
  backwards - this is the same presence-detection idiom edx-platform's own
  `resolve_storage_backend()` uses). Profile images get the same full S3
  treatment as Verawood/Ulmo in both dev and production: a thin wrapper
  storage class (referenced via a self-referential `__name__`-based path)
  strips the `base_url` key Teak's `lms/urls.py` still needs before
  delegating to `S3Boto3Storage`. Verified live against a real Tutor 20.0.5
  environment: LMS, CMS and Discovery boot cleanly and read/write through
  all three backends, in both `tutor dev` and `tutor local launch`; the
  existing Ulmo/Verawood behavior is unchanged (same live re-check, same
  passing test suite). `pyproject.toml`'s `tutor` dependency now reads
  `>=20.0.0,<23.0.0`.

- [Bugfix] Profile images never reached object storage under `tutor dev`,
  in all three backends - two compounding bugs, both masked by `tutor
  local` never hitting the code paths that expose them. (1) Each backend
  registered its override under Tutor's `openedx-lms-production-settings`
  patch point, which only renders into `lms/production.py` - renamed to
  `openedx-lms-common-settings` (also what tutor-contrib-s3 uses). (2)
  Once applied in dev, the override's full replacement of
  `PROFILE_IMAGE_BACKEND` broke `lms/urls.py`'s dev-only `DEBUG=True`
  static-serving route, which reads that setting directly and expects a
  `base_url` key `S3Storage` doesn't accept. Fixed by leaving
  `PROFILE_IMAGE_BACKEND` untouched and setting `STORAGES["profile_image"]`
  instead - `get_profile_image_storage()` prefers it when present. New
  regression test renders the full `lms/development.py` /
  `lms/production.py` templates and checks the override's own dict.

- [Bugfix] The `rustfs` backend's console login failed with
  `SignatureDoesNotMatch: missing signed header: content-type`, which
  looked like a credentials problem but wasn't one - found live, debugging
  a real login attempt. RustFS's own console frontend sends signed
  STS/S3 calls straight to the bare root (`POST /` for the login
  handshake, `GET /?x-id=ListBuckets` for the dashboard), and the
  Caddyfile's `redir / /rustfs/console/ 302` - meant only for a human
  navigating to the bare host - intercepted those too, unconditionally:
  confirmed live with `curl -X POST /` and a (deliberately invalid)
  `Authorization` header, which got redirected exactly like a plain
  browser visit instead of reaching RustFS. The browser then followed
  that redirect still carrying the original signature, which no longer
  matched the path RustFS actually received, surfacing as a signature
  error instead of a routing one. Fixed by scoping the redirect to a
  Caddy matcher that excludes requests carrying an `Authorization` header
  or a presigned `X-Amz-Signature` query parameter - verified live that
  signed requests now reach RustFS directly (getting RustFS's own, correct
  403/400 for an invalid signature) while a plain `GET /` still redirects
  to the console as before. New regression test in `test_patches.py`
  confirmed red against the old unconditional redirect before being
  restored to green against the fix.

- [Bugfix] `EVALUATION.md` Finding 1 (the out-of-order multipart hang,
  documented since this plugin's initial release as "do not use for
  production course content until this is understood or fixed upstream")
  has been root-caused and mitigated, not merely documented. Reading
  rclone's own source (`cmd/serve/s3/multipart.go`) after reproducing the
  hang live and capturing a Go goroutine dump showed it blocked in
  `waitForTurn` on a `sync.Cond.Wait()` - a genuine request-level deadlock,
  not a spin: `serve s3` writes multipart uploads through the VFS strictly
  in part order, holding out-of-order parts in a bounded in-memory reorder
  buffer (rclone's own `--multipart-streaming-buffer-limit`, default
  256MiB, "0 for unlimited"); a client that delivers parts in strict
  descending order one at a time fills that buffer before the part that
  would drain it ever arrives.

  New config key `OBJSTORAGE_AZURE_MULTIPART_BUFFER_LIMIT` (default `"2G"`)
  raises this on the authenticated `rclonegw` only (`rclonegw-public` is
  `--read-only` and never reaches this code path). Re-ran the full original
  adversarial test matrix (300-350 MiB, concurrency 1/2/4/8/10, strictly
  reversed part order) against the raised limit: every case that
  previously hung now passes, with correct final object size and zero
  residual CPU usage. This is a narrowed risk, not an eliminated one - an
  upload larger than the configured buffer, delivered in sufficiently
  reversed order, can still hit the same wait, and operators with larger
  course videos should raise this further. `EVALUATION.md`'s headline
  summary, Finding 1, the backend comparison table, and the recommendation
  section are all rewritten to describe the actual mechanism and its fix
  instead of "no known fix, don't use this backend for real content."
  `TESTING.rst` and the CI workflow are updated to match, including
  promoting the previously manually-gated adversarial test
  (`OBJSTORAGE_TEST_REAL_AZURE_GATE=1`) to run automatically on every PR
  (pinned to 350 MiB) as the standing regression guard for this fix.

- [Bugfix] The `azure` backend's `rclonegw`/`rclonegw-public` processes ran
  with `--dir-cache-time=0`, which does not mean "caching disabled" - it
  means every directory-cache entry is immediately expired, which drives
  rclone's VFS layer's per-directory cleanup timer to re-arm itself with no
  effective delay. Root-caused via a live Go goroutine dump
  (`docker kill --signal=SIGQUIT`) showing a storm of
  `vfs.(*Dir).cacheCleanup`/`ForgetAll` goroutines deadlocked on the same
  directory's mutex, pinning the gateway at 100%+ CPU until it stops
  responding to every request - triggered by ordinary Studio usage
  (exporting a course containing a SCORM component), not adversarial
  behavior. An initial fix changed `--dir-cache-time` from `0` to `1s`,
  which cleared the livelock (4/4 reproducible hangs at `=0`, 9/9 clean runs
  at `=1s`/`=5s`) but introduced its own regression, caught before merging:
  `rclonegw`'s `HeadObject`/`GetObject` resolve a key via the same
  per-directory cache the cleanup storm came from, so a full second of
  caching was long enough for an ordinary `PutObject` immediately followed
  by a `HeadObject`/`GetObject` for the same key to intermittently return
  `404`/`NoSuchKey` - breaking this plugin's own live conformance suite in
  CI. Settled on `--dir-cache-time=10ms`: short enough that the
  write-then-read regression did not reproduce across repeated full
  conformance-suite runs locally, while still clearing the original
  livelock trigger (the real Studio/SCORM export) 4/4 further attempts.
  Applied to `patches/azure/local-docker-compose-services`,
  `patches/azure/k8s-deployments`, and the CI compose stand-in. This also
  **corrects** the previous `[Finding]` entry below (and the
  `EVALUATION.md` Finding 5 it introduced): that entry's claim of a
  minimal, standalone boto3 reproduction did not hold up under further
  testing - the apparent single-call reproduction was an artifact of
  replaying the same call against a gateway a stuck client was still
  hammering in a retry loop, not evidence that any single isolated call
  triggers this on its own. `EVALUATION.md` Finding 5 has been rewritten to
  describe the actual root cause, both corrections, and the fix - including
  an explicit note that `10ms` narrows the staleness window rather than
  eliminating it, and what to look for if it's ever hit in practice.

- [Finding] A second full live-content pass through all three backends -
  this time through the actual running Open edX application (a real login,
  a real profile-picture upload, and a real Studio course export/import
  round trip), not just direct S3-API calls - surfaced a second rclone
  livelock, independent of the multipart one in `EVALUATION.md` Finding 1:
  **a single `ListObjects`/`ListObjectsV2` call with a `Delimiter` against a
  prefix that has zero objects under it hangs the `azure` backend's
  `rclonegw` gateway indefinitely**, pinning it at 140-165% CPU with no
  self-recovery. No multipart upload, size threshold, or adversarial client
  behavior is required - this is the "does this folder exist yet" check
  `django-storages`' `S3Boto3Storage.listdir()` performs, and it was found
  through nothing more unusual than exporting a real course containing a
  SCORM component from Studio's own UI. Confirmed twice, independently
  (against both the bundled Azurite and an unrelated emulator,
  [floci-az](https://floci.io/az/)), isolated to a minimal boto3
  reproduction with no Django/Celery/SCORM involved, and confirmed that a
  gateway restart alone does not recover it if the original stuck client is
  still retrying. See `EVALUATION.md` Finding 5 for the full writeup and
  `TESTING.rst`'s failure-modes table for the updated recovery steps. Not
  something this plugin's code can fix - the defect is in `rclone serve
  s3` itself - so, matching this repository's existing practice for
  Finding 1, this is a documentation update, not a code change.

- [Bugfix] The `tutor` dependency's upper bound was `<22.0.0`, which
  silently downgrades an existing Tutor 22.x install to the newest 21.x
  release on `pip install tutor-objectstorage` - breaking every other
  installed Tutor plugin's own `tutor<23.0.0,>=22.0.0` constraint in the
  process, with `pip` only ever surfacing this as a dependency-conflict
  *warning*, not an install failure. Nothing in this plugin's code actually
  requires Tutor 21: every command in `TESTING.rst` Tiers 2-4 (`tutor
  plugins enable`, `config save`, `local do init`, `local start`) was
  confirmed live against Tutor 22.0.2 with no changes needed. Widened to
  `<23.0.0`, matching the bound every other current Tutor plugin uses, with
  a regression test (`test_tutor_dependency_does_not_exclude_current_tutor_releases`)
  that reads the installed distribution's own dependency metadata, so a
  future re-narrowing of this bound fails loudly in CI instead of only
  showing up as a silent downgrade after install.

- [Finding] This plugin had never actually been run before this pass -
  previous changelog/testing claims describing live verification during
  development predate any real execution. First genuine end-to-end run,
  covering all three backends: `external_s3` and `azure` pointed at
  [floci.io](https://floci.io)'s local AWS S3 and Azure Blob Storage
  emulators respectively, `rustfs` run locally, all through the real
  Tutor-rendered Caddy + backend stack (not a hand-rolled stand-in),
  exercising put/get, multipart upload, presigned URLs, list, copy, delete,
  and anonymous public/private bucket routing. All three backends passed.
  One gap surfaced in `floci-az` itself (no `Copy Blob` support, so
  multipart completion and `CopyObject` fail there) - confirmed as an
  emulator limitation, not a plugin bug, by reproducing the identical
  operation successfully against this plugin's own bundled Azurite
  emulator with zero code changes, and independently against the Azure SDK
  directly.

- [Feature] Initial release. `tutor-objectstorage` unifies three previously
  separate ways of configuring Open edX's object storage behind one config
  key, `OBJSTORAGE_BACKEND`:

  - `rustfs` (default) — a local, self-hosted RustFS server. Successor to
    the standalone `tutor-rustfs` plugin.
  - `azure` — Azure Blob Storage via rclone's `serve s3` translation layer.
    Successor to the standalone `tutor-rclone-gateway` plugin. Gated behind
    a second, explicit `OBJSTORAGE_AZURE_ACKNOWLEDGE_RISK` setting — see
    below and `EVALUATION.md`.
  - `external_s3` — any external S3-compatible provider (AWS S3, GCS,
    Ceph, a RustFS/MinIO instance elsewhere). Reimplements the
    functionality of [tutor-contrib-s3](https://github.com/cleura/tutor-contrib-s3),
    an independently maintained plugin this one does not depend on or
    replace, so that this plugin is a complete, standalone choice.

  Selecting a backend you aren't using costs nothing: that backend's
  compose/Kubernetes resources, Caddy routing, and init-task logic simply
  don't render for the other two. `tutor-rustfs`, `tutor-rclone-gateway`,
  and `tutor-minio` all conflict with this plugin and cannot be enabled
  alongside it. See `MIGRATING.rst` for moving an existing `tutor-rustfs`
  or `tutor-rclone-gateway` deployment onto this plugin.

  - 💥 **One shared generated secret, not one per backend.** An earlier
    design generated a separate `CONFIG_UNIQUE` secret per backend and
    dispatched `OPENEDX_AWS_SECRET_ACCESS_KEY` between them with a
    `{% if OBJSTORAGE_BACKEND == ... %}` expression. This broke
    `tutor config save` outright: Tutor renders `CONFIG_OVERRIDES` during
    an early `load_minimal()` pass that happens *before* plugin
    `CONFIG_DEFAULTS` — including `OBJSTORAGE_BACKEND` itself — are merged
    in, so the backend-conditional expression raised "is undefined" on
    every save. Worse, papering over the crash would still have been
    wrong: whatever fallback branch fired would have been permanently
    written into `config.yml` on the very first save, regardless of which
    backend was actually selected, with no later re-evaluation. Fixed by
    using one shared `CONFIG_UNIQUE` secret and an unconditional override —
    correct because only one backend's infrastructure is ever actually
    running at a time, so there is nothing to isolate. Caught by actually
    running `tutor config save`, not by the unit test suite alone.
  - 💥 **The fuller 6-bucket model (main, file-upload, video-upload,
    grades, openedx-learning, discovery) now applies to all three
    backends**, including `external_s3`, which previously (under
    `tutor-contrib-s3`) had a narrower 4-bucket model. An `external_s3`
    deployer migrating from `tutor-contrib-s3` needs to provision 2 more
    buckets. The profile-image-stays-on-local-filesystem capability
    `tutor-contrib-s3` offered (`S3_PROFILE_IMAGE_BUCKET=""`) is preserved
    exactly, as `OBJSTORAGE_EXTERNAL_S3_PROFILE_IMAGE_BUCKET=""` — a strict
    superset of the old behavior, not a replacement.
  - 💥 **The init task's job/service name now matches the plugin's own
    Tutor entry-point name** (`objectstorage` → `objectstorage-job`, for
    every backend). `tutor-rclone-gateway`'s init task was registered under
    a different name than its plugin name (`rclonegw` vs. `rclonegateway`),
    so `tutor local do init --limit=rclonegw` silently matched nothing.
    Making these identical removes that trap instead of reproducing it
    under a new name. One consequence: the `external_s3` backend, which
    provisions nothing, still has to contribute a minimal stub job
    (`image: docker.io/library/alpine:3`) to this shared slot, since
    Tutor's job runner resolves `<service>-job` unconditionally regardless
    of which backend is active.
  - [Security] The `azure` backend's rclone image stays hard-pinned to an
    exact tag (`docker.io/rclone/rclone:1.75.1`), not a floating range, for
    the same reason it was pinned in `tutor-rclone-gateway`: 1.75.1 fixes
    CVE-2026-88045, an unauthenticated memory-exhaustion denial-of-service
    in rclone's multipart upload path, introduced in the same 1.75.0
    release that rewrote multipart streaming. Bumping this requires
    re-running the live conformance suite against the new tag first — see
    `EVALUATION.md`.

- [Improvement] A new cross-backend containment test suite
  (`tests/test_patches.py`) — the one genuinely new test class this merge's
  modular, self-guarded-fragment-per-backend design needs: it renders every
  infrastructure-relevant patch for each of the three backends and asserts
  that none of the other two backends' markers (service names, CLI flags,
  env var names) leak into the output. Neither predecessor plugin's test
  suite could express this, because neither ever had to render a second
  backend's content side by side with its own. Also carries forward both
  predecessor plugins' own hardened test suites in full: hand-maintained
  `EXPECTED_KEYS` sets, legacy-prefix leak guards, the Caddyfile
  discovery-clause regression guard, and the dynamic
  `test_patch_names_are_real_tutor_hooks` check that greps installed Tutor
  core/sibling packages for real `patch(...)` hook names (this is what
  originally caught `tutor-rclone-gateway`'s `k8s-networkpolicy` naming
  bug during its own development).

- [Documentation] `EVALUATION.md` carries forward `tutor-rclone-gateway`'s
  full evaluation of the `azure` backend (the multipart livelock finding,
  the non-standard anonymous-request error code, the Range-request status
  quirk) rescoped as one backend's evaluation within this plugin rather
  than a whole separate plugin's. `TESTING.rst` merges both predecessor
  plugins' tiered procedures into one, adding backend-switching examples
  and an `external_s3` tier describing how that backend was verified
  during development (against a standalone S3-compatible stand-in
  container, since "live" has no one-size-fits-all meaning across external
  providers).

- [Finding] Live-testing this merge (not just rendering templates) verified
  all three backends end-to-end, including through the real Caddy
  container for the `azure` backend's public/private routing split (not
  just at the container level): bucket provisioning, authenticated and
  anonymous read/write, correct rejection of anonymous writes and of
  anonymous reads on private buckets, a real in-order multipart upload,
  and — for `external_s3` — a full round trip against a standalone
  S3-compatible stand-in with no Tutor-managed infrastructure at all. The
  `azure` backend's known multipart livelock (see `EVALUATION.md` Finding
  1) was not re-triggered deliberately during this merge; it was already
  proven in `tutor-rclone-gateway`'s own evaluation and nothing about the
  merge changes the pinned rclone image or its command-line flags.
