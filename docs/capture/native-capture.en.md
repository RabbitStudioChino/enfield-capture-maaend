# Native Same-Frame Completion and Passive Writing

Updated 2026-11-09. C02 adds completion subscriptions to the existing `PositionProvider` and optional passive diagnostic recording through `MapNavigateAction`. The task-layer CLI owns manual capture and formal package delivery. Native diagnostic data uses a separate format.

Automatic capture now uses `CaptureViewAction`: camera-only yaw control, arrival and settling gates, raw PNG plus localization derived from the same image, and a native process clock. The dedicated worker calls it after navigation exits and turns its receipt into a formal CaptureBundle. See [automatic region capture](automatic-capture.en.md). All tests for this addition are skipped; the checks below describe earlier passive-recording work.

## Completion Subscription

Each `capture()` and `captureForStrictGoal()` call assigns a current `session_id`, `clock_id`, and increasing `frame_id`. Each provider owns a separate session and UTC / monotonic anchors, sharing its clock across attempts within that scope. Completion records include host monotonic screenshot request, screenshot completion, localization start and end, and overall completion times. `native_render_timestamp` remains `null`, and `frame_freshness` is `unknown`.

The completion path covers null output parameters, screenshot failure, missing images, minimap extraction failure, localization failure, precise-position rejection, missing headings, and exceptions. `LocateResult` retains the current frame's raw position, character heading, and camera heading. Positions before and after normalization are recorded separately. Callers can identify the transform with `setPositionNormalizer(normalizer, normalization_id)`. Existing navigation callers leave this identifier `null`. Localization does not measure height, so `floor_y` in completion snapshots is `null`.

The existing `setFrameObserver()` runs before localization, and strict-goal black-screen checks still precede that observer. The new `subscribeCompletion()` returns a noncopyable scope token. Each provider accepts one completion subscription at a time. Registration, reset, and provider calls stay on the existing navigation thread. A token can safely outlive its provider.

Completion callbacks synchronously receive `const FrameCompletion&` and a borrowed `const cv::Mat&`, whose pixels remain valid until the callback returns. Async consumers reserve queue capacity before copying the image. The provider isolates completion-callback exceptions. Existing observer, locator, and normalizer exceptions continue through their original call paths.

## Bounded Writing and Failure

`BoundedFrameWriter` uses one background thread, with simultaneous defaults of eight frames and 256 MiB of uncompressed images. Reservations include cloning, queued, and in-flight images. `clone()` follows successful reservation. Full queues, oversized frames, unsupported image formats, and submissions after stopping immediately return a specific `SubmitResult`. The caller also holds the corresponding completion record and `frame_id`.

`stats()` exposes accepted, written, dropped, and aborted counts, current reservations, high-water marks, and errors. Normal stopping drains accepted frames. `stop(false)` discards queued frames and allows the current write to finish. Disk or encoding failure closes admission, records the failure, and notifies the error callback. Background callbacks should only signal the coordinator.

`NativeFrameStore` writes a PNG `.partial` in the background, flushes and syncs it before renaming, then appends and syncs JSONL. A provider's frame sequence can have gaps caused by drops. Duplicate or out-of-order records are rejected. Each diagnostic session creates a new directory, protecting existing directories and files from overwrite. Failures retain written data, temporary files, and possible orphan images for local diagnosis.

`summary.json` preserves the total drop count, last dropped frame ID, and reason. This passive diagnostic provides a bounded drop summary. Per-rejection audit details can be extended when formal tasks attach. Directories with `.partial` files, missing summaries, or failed summaries should be retained as unfinished diagnostics. The task layer owns formal `CaptureBundle` freezing, hash manifests, and recovery validation.

## Enabling Through Existing Navigation

Build the updated `cpp-algo` and load that version in the host. Set the absolute real workspace path in the PowerShell used to start the host, then run an already configured `MapNavigateAction` through its existing entry point.

```powershell
$env:MAAEND_NATIVE_CAPTURE_WORKSPACE = (Resolve-Path '.').Path
```

Output goes to that workspace's `local/native-capture/<session_id>/`, containing `session.json`, `frames.jsonl`, `images/`, and `summary.json` after stopping. The workspace and output directories must be real directories. Symlinks and Windows reparse points along the path are rejected. An unset environment variable preserves existing navigation behavior.

`PassiveRecorder` owns the writer and subscription inside the navigation call, resetting the subscription before draining. Background write failures only set an atomic fault flag. The navigation thread follows the existing `should_stop` path to stop scanners and release movement input. Write or summary failure during final draining also makes that navigation call return failure. Passive recording observes existing navigation screenshots. Automatic photography instead uses the dedicated serial task path described above, without mixing passive diagnostics into formal exports.

## Local Verification and Hardware Boundaries

This workspace completed a full production `cpp-algo` compile and link using official MaaDeps v3.0.0, the MaaFramework 5.14.3 macOS arm64 SDK, OpenCV 4.12.0, and AppleClang 21. Independent regressions use real SDK image buffers, OpenCV, PNG, and JSONL, with test doubles injecting screenshot requests and localization results. Coverage includes all completion exits, existing observer order, exceptions and token lifetime, owned memory, capacity including in-flight images, rejection, draining, aborting, disk errors, and Unicode paths.

New tests, build copies, dependencies, samples, and logs remain under workspace `local/`. The independent test entry point is `local/qa/capture/native/`, built as a separate CMake target with its test `main` outside production recursive source collection. Native Windows execution, actual localization, and passive recording during sustained navigation still await hardware evidence. Independent task development can continue meanwhile.
