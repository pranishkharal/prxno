# Infrastructure layer (`infra/`)

Production-grade plumbing for *Auto Clips for Kick*. It is **additive**: the
existing bot, its commands and its features are unchanged, but the fragile parts
underneath were replaced with components that are safe under concurrency.

## Defects this layer fixes (all verified in the original source)

| Original defect | Evidence | Fix |
| --- | --- | --- |
| Concurrency hardcoded and hardware-blind | `main.py:500-503` (`10` edits / `5` downloads) | `infra/concurrency.py` derives limits from the machine |
| Whisper and FFmpeg shared one semaphore | `run_encode_job` used for both `edit_video` and `transcribe_and_caption` | separate `download` / `analysis` / `encode` classes |
| No source dedup; cache identity was the **user** | `main.py:854` `uploads/kick_{user_id}` | `infra/source_cache.py` keys on the KICK clip id |
| Two jobs for one user wiped each other | `main.py:856-859` | unique per-job folder + hard link |
| A truncated download could look complete | `main.py:878` `"nopart": True`, only a `<10 000` byte check | `.part` re-enabled, plus size **and** decodability validation |
| Jobs deleted shared/other-job files by name prefix | `cleanup_job_files` (`main.py:1047-1051`) | cache owns its files; jobs only ever delete their own link |
| Failed jobs leaked; cleanup targeted a filename that never existed | `main.py:4153,4438` `kick_clip.mp4` vs real `kick_clip_<id>.mp4` | `cleanup_stale_kick_uploads()` (age-based, safe) |
| Analysis cache keyed by random job id, so reuse was impossible | `job_manager.py:201`, `pipeline.py:122,207,255,339` | `infra/analysis_cache.py` keys on source + model + version + config |
| Running FFmpeg could not be cancelled | `main.py:514-581` blocking loop, no registry | `infra/ffmpeg_runner.py` process registry + `cancel()` |
| Success inferred from the return code alone | same | outputs validated (exists / non-empty / decodable) |
| Whisper singleton was not thread-safe | `captioning.py:20-43` | `infra/whisper_manager.py` (lock + inference serialisation) |
| No job correlation, only `print` | throughout | `infra/logutil.py` structured JSONL events |

## Layout

```
infra/
  config.py          env-driven configuration (safe defaults)
  logutil.py         structured, job-correlated events; secret redaction
  hardware.py        CPU/RAM/GPU detection + encoder verification, cached
  concurrency.py     adaptive per-class limits + semaphores (ResourceGovernor)
  locking.py         atomic publish, cross-process locks, media validation
  source_cache.py    deduplicating source cache (stable clip-id identity)
  analysis_cache.py  versioned transcript / analysis cache
  workspace.py       isolated per-job directory trees
  ffmpeg_runner.py   process registry, timeout, cancel, output validation
  whisper_manager.py one shared, thread-safe model
  jobs.py            durable JobStore + resource-aware JobQueue
```

### Request flow (target)

```
Discord command
   -> JobRecord (durable, job_id)
   -> ResourceQueue[class]            (adaptive limits)
   -> SourceCache.fetch()             ONE download for N users of a source
        -> validate -> os.replace()   atomic publication
   -> AnalysisCache.get_or_compute()  ONE transcript/analysis per source
   -> per-job workspace + hard link   N independent edits
   -> FFmpegRunner.run()              timeout / cancel / validate
   -> Discord delivery / public link
```

## What is wired into the bot today

* `run_ffmpeg()` delegates to `FFmpegRunner` (same signature, same console output).
* `download_kick_clip()` goes through `SourceCache.get_for_job()`.
* `run_encode_job` / `run_download_job` / `run_analysis_job` use the governor.
* Caption generation runs in the **analysis** class, not the encode class.
* `captioning.py` uses the shared `WhisperManager`.
* Startup primes hardware detection and runs cache maintenance sweeps.
* VOD/clip/live pipelines run through `spawn_tracked_job()` onto the durable
  `ResourceQueue`, so jobs survive restarts and `!cancel` stops real work.
* VOD transcription is deduplicated per source via
  `PipelineOrchestrator._transcribe_shared()` (`canonical_source_id` plus the
  versioned `AnalysisCache`); per-job transcript/analysis files remain as job
  record pointers.

## Still future work

* `workspace.py` in the edit path: edits still write into `output/`; the
  isolated per-job workspace primitive exists and startup already sweeps
  stale workspaces.

## Configuration

All optional; defaults are safe.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CACHE_ROOT` | `./cache` | source + analysis cache root |
| `JOB_ROOT` | `./temp/jobs` | per-job workspaces |
| `LOG_DIR` | `./logs` | JSONL event log |
| `CACHE_TTL` | `86400` | cache retention (seconds) |
| `TEMP_MAX_AGE` | `21600` | stale workspace / temp age |
| `FFMPEG_TIMEOUT` | `1800` | per-FFmpeg hard timeout |
| `JOB_MAX_ATTEMPTS` | `3` | retries for transient failures |
| `JOB_RETRY_BACKOFF` | `2.0` | backoff base between retries |
| `MAX_CONCURRENT_EDITS` | auto | override the encode budget |
| `MAX_CONCURRENT_DOWNLOADS` | auto | override the download budget |
| `MAX_CONCURRENT_ANALYSIS` | auto | override the Whisper budget |
| `AUTO_HW_ENCODE` | `false` | `true`/`auto` enables a **verified** hardware encoder |
| `WHISPER_MODEL` | `base` | transcription model |
| `WHISPER_DEVICE` | auto | `cuda` when torch reports CUDA, else `cpu` |
| `WHISPER_COMPUTE_TYPE` | auto | `float16` on CUDA, `int8` on CPU |
| `WHISPER_BACKEND` | `openai` | `openai` or `faster` |

## CPU and GPU behaviour

Hardware encoding is **opt-in** (`AUTO_HW_ENCODE=false`) on purpose: switching
`libx264` to `h264_nvenc`/`qsv`/`amf` changes the rate-control model
(`-crf` becomes `-cq`/`-global_quality`) and could subtly alter the output of a
working production bot. Detection, verification and substitution are fully
implemented, so enabling it is one environment variable.

An encoder is only offered after a **real encode probe**: FFmpeg must produce a
non-empty file that decodes back to at least one frame. Exit code `0` alone is
not trusted. If nothing verifies, the CPU path is used automatically.

Measured on the development machine (16 cores / 15.1 GB / RTX 3050 / CPU-only torch):

```
h264_nvenc  driver too old, rejected
h264_qsv    no MFX session, rejected
h264_amf    verified (valid 15-frame H.264 produced)
cuda        False -> Whisper runs on CPU
limits      downloads=8  analysis=2  encodes=6
```

## Tests

Standard library only (pytest is not installed in this environment):

```
python tests/run_all.py
```

98 tests cover cache miss/hit, concurrent same-source requests (one download),
truncated and undecodable payloads, cache recovery, distinct sources, two jobs
sharing a source, cleanup isolation, FFmpeg failure/timeout/cancellation, job
retry, permanent-failure classification, cancellation, restart recovery, and
static integration guards proving `main.py` is actually wired to this layer.

## Multi-machine / Redis seam

`JobStore` is deliberately backend-shaped (`save` / `load` / `all` / `delete` /
`repair_orphans`). A single machine needs no Redis, so the default is local JSON.
For multiple worker machines, add a store implementing the same interface plus a
shared queue; `ResourceQueue` already separates worker identity
(`JobRecord.worker`) and the resource classes.