# RTSP Studio (RIG-02) REST API

The interactive OpenAPI documentation is served by the backend at **`/docs`** (Swagger UI) and **`/openapi.json`**. By default there is no login and every endpoint is open to anyone who can reach the server. With `RTSPM_AUTH_ENABLED=true`, every endpoint except `/api/health` requires authentication: either the browser session cookie from `POST /api/auth/login`, or HTTP Basic auth (`curl -u user:password`). Write operations require the `operator` (or `admin`) role; `viewer` users get 403. **Protected** streams can only be changed by their owner or an admin (403 names the owner and their note). Failed logins are throttled per user (5 free attempts, then exponential lockout) and per client address (30), answered with **429** and `Retry-After`.

Errors are JSON `{"detail": "<human-readable message>", ...}` with a meaningful status:

| Code | Meaning |
|---|---|
| 400 | malformed request (e.g. bad `If-Match`) |
| 401 / 403 | not logged in / read-only user |
| 404 | not found (e.g. stream deleted by another user) |
| 409 | conflict: duplicate name, stale `If-Match` version, capacity reached, already running |
| 411 / 413 / 415 | upload without size / too large / unsupported type |
| 422 | invalid input: name, times, unusable or incompatible video, source unreadable (`field` says which input) |
| 503 | `/api/health?strict=true` when degraded; snapshot when the stream cannot be read |
| 429 | too many failed logins (see `Retry-After`) |
| 507 | not enough disk space (`required`, `free`, `reserve`, `available`, `reason`) |

## Optimistic concurrency

Every stream has a `version`. Send it as `If-Match: <version>` on PATCH/start/stop/restart/DELETE. If someone else changed the stream meanwhile, the server answers **409** "changed by <user>" instead of overwriting their change. The dashboard always sends it; scripts may omit it.

## Endpoints

Requests that arrive through Cloudflare (a `Cf-Ray` / `Cf-Connecting-Ip` header) need a valid Cloudflare Access token (`Cf-Access-Jwt-Assertion` header or `CF_Authorization` cookie), else 403; the verified e-mail is the acting user. See DEPLOYMENT.md §4c.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health[?strict=true]` | Liveness: `{status: ok|degraded, mediamtx, recovery, version}` (no auth) |
| POST | `/api/auth/login` · `/api/auth/logout` · GET `/api/auth/me` | Session handling |
| POST | `/api/auth/password` `{current_password,new_password}` | Change your own password |
| GET/POST | `/api/users` · PATCH/DELETE `/api/users/{name}` | **Admin**: list/create users, change role or reset password, delete (the last admin is protected) |
| GET | `/api/config` | Public RTSP host/port, `rtsp_hosts` (address choices: canonical, alternates, the address this request used), `server_addresses`, URL template, name rules, limits, output profile, warnings |
| GET | `/api/videos?search=&status=&include_missing=` | Library with FFprobe metadata and compatibility verdict. Uploads made through the app also carry `uploaded_by` (the signed-in user, or `anonymous` with login off), `uploaded_at`, `uploaded_from` (client address; `X-Forwarded-For` is trusted only from loopback) and `uploaded_device` (e.g. "Windows · Chrome", "curl"); all null for files copied into the folder |
| POST | `/api/videos/refresh` | Rescan the server folder now |
| POST | `/api/videos/upload/check` `{filename,size}` | Pre-flight: name and disk-space check, no data sent |
| POST | `/api/videos/upload?filename=` (raw body) | Streamed single-request upload (`Content-Length` required) |
| POST | `/api/videos/uploads` `{filename,size}` | Start a **resumable** upload: reserves disk space, returns `{id, offset}` |
| PUT | `/api/videos/uploads/{id}?offset=N` (raw body) | Append bytes; 200 `{offset}` while incomplete, 201 + video when done, 409 `{offset}` on a wrong offset |
| GET/DELETE | `/api/videos/uploads/{id}` | Ask the current offset (after a dropped connection) / abort and release the space |
| DELETE | `/api/videos/{id}` | **Admin**: move to `.trash` (purged after 7 days); 409 while any stream uses it |
| GET | `/api/videos/{id}` · `/thumbnail` · `/frame?t=` | Metadata, thumbnail, frame at time *t* (JPEG) |
| GET | `/api/videos/{id}/file` · `?download=true` | The video file: plays in the browser (HTTP Range, seekable) or downloads under its own name. Any signed-in role, viewers included |
| GET | `/api/videos/{id}/preview?start=` | A short H.264 clip (`RTSPM_PREVIEW_SECONDS`, up to 720p) from `start` seconds, for playing long videos in the browser. Made on first request, then cached (`RTSPM_PREVIEW_CACHE_BYTES`, least recently used removed first; removed with the video). Header `X-Preview-Start`: the clip's real start (clamped to leave a full clip) |
| POST | `/api/videos/{id}/convert` | Operator: make a copy next to the original (kept). Optional body `{"profile": "h264" \| "720p" \| "16x9", "fit": "blur" \| "pad" \| "crop"}`. `h264` (default): universal H.264 copy `<name> (h264).mp4`, plays in every browser, streams without live transcoding, keyframe every 2 s. `720p`: a 16:9 video at the standard size, `<name> (720p).mp4` (Standard streams use it automatically). `16x9`: a portrait/4:3/other video fitted into the standard 16:9 frame with blurred bars, black bars or a crop, `<name> (720p 16x9).mp4`. Background job (202); 409 if running, already converted, a converted copy, or the wrong shape for the profile; 507 if the disk is too full |
| GET | `/api/naming` | The video naming convention (`WHAT_WHERE_NN[_tag].ext`): codes, rules, examples, current mode |
| POST | `/api/naming/check` `{name}` | Is the name valid? Reasons, and a conforming suggestion when it is not |
| POST | `/api/naming/suggest` | Full name for WHAT + WHERE (+ tags) with the next free clip number |
| PUT | `/api/settings/naming` `{mode}` | **Admin**: `enforce` (uploads with other names refused), `warn` or `off`; live, no restart |
| POST | `/api/videos/{id}/rename` `{name}` | Rename a video to a convention name; streams using it keep running, their names and URLs don't change |
| POST | `/api/videos/rename-batch` | **Admin**: rename many (e.g. from `docs/naming/video-renames-proposed.csv`); dry run by default |
| GET | `/api/videos/conversions` | Recent and running conversions: state (queued, running, done, failed, cancelled), progress 0–1, eta_s, error, result_video_id |
| DELETE | `/api/videos/conversions/{job}` | Operator: cancel a conversion (temp file removed) |
| GET | `/api/streams` | All streams with live runtime (status, path ready, viewers with host names, `viewer_peak_today`, `preview_watchers`, loops, progress, `start_queue` {position, eta_s} while queued for a start slot, `spacing_warning`, `lead_in` {mode, phase, video_in_s, viewer_seen_at, viewer} during a lead-in, `slate_on_url`); statuses include `LEAD_IN` and `WAITING_FOR_VIEWER` |
| POST | `/api/streams/validate` | Dry run of a create: effective (keyframe) start/end, output mode, warnings |
| POST | `/api/streams[?wait=true]` | Create (and start unless `start_now:false`); optional `slot_id` links it to an analytics video placeholder; `lead_in_mode` (`none`/`fixed`/`hold`, default from `RTSPM_DEFAULT_LEAD_IN_MODE`), `lead_in_s` (0–120), `hold_max_s`; `output_size` (`standard` = `RTSPM_OUTPUT_WIDTH`x`HEIGHT`, `original`, or a `WxH` from `RTSPM_STANDARD_SIZES`; default `RTSPM_OUTPUT_DEFAULT`, which is `original`, the file's own size): Standard uses the video's ready `(720p)` copy when there is one, else scales live. Videos that are not 16:9 are refused (422) while `RTSPM_REQUIRE_16_9=true`. The response has `output_size` and `output_resolution`. `clock` (bool) and `clock_tz` (`IST`/`UTC`) burn the server time into every frame (the stream is then transcoded). `?wait=true` waits through a fixed lead-in; a hold stream returns `WAITING_FOR_VIEWER` |
| GET | `/api/streams/{id}` | Detail: command, PID, config, source metadata, last 20 log lines, events |
| PATCH | `/api/streams/{id}` | Edit (`If-Match`). Config changes restart a running stream; `note`, `protected` (owner/admin), `owner` (admin) and the lead-in fields (`lead_in_mode`, `lead_in_s`, `hold_max_s`, `clear_hold_max`; applied at the next start) do not. Renaming requires a stopped stream |
| POST | `/api/streams/{id}/start|stop|restart[?wait=true]` | Lifecycle (start and restart run the stream's lead-in) |
| POST | `/api/streams/{id}/go` | "Start video now": end the lead-in (409 if the stream isn't in one) |
| DELETE | `/api/streams/{id}` | Stop and remove the stream; **never deletes the source video** |
| GET | `/api/streams/{id}/logs?lines=20` | Log tail (1–200 lines) |
| GET | `/api/streams/{id}/snapshot` | One JPEG frame read back over RTSP (end-to-end check; counts as a reader briefly) |
| GET | `/api/streams/{id}/preview` | Thumbnail of the current playback position, from the source file (no RTSP reader); cached 20 s |
| GET | `/api/streams/{id}/live/{file}` | Live preview: HLS playlist and segments (`index.m3u8`) proxied from MediaMTX's loopback HLS server, behind the dashboard login; 409 unless the stream is live |
| GET | `/api/streams/{id}/history?hours=24` | Status segments and `running_percent` (availability) |
| POST | `/api/streams/bulk` `{stream_ids, action}` | start/stop/restart/delete many; result per stream (protected ones are reported, not changed) |
| GET/POST | `/api/groups` · DELETE `/api/groups/{id}` | Stream groups ("demo sets"); deleting a group keeps its streams |
| POST | `/api/groups/members` `{stream_ids, group_id}` | Put streams into a group (`null` removes them) |
| POST | `/api/groups/{id}/start\|stop\|restart` | Act on every stream of a group |
| GET | `/api/groups/{id}/urls?format=txt\|csv&host=` | All RTSP URLs of a group (for the portal or `measure_recovery.py`). `host` must be one of `rtsp_hosts` (else 400) |
| GET | `/api/analytics` | Analytics tree (Overview page and analytics pages): categories → analytics with `slots`, `videos` (distinct library videos), `slot_ids`, `streams`, `running`, `problems`, `viewers` |
| GET | `/api/analytics/suggestions` | Videos whose file name suggests an analytics they are not in yet: `[{video_id, rel_path, analytics_id, analytics_name, word}]` (e.g. `Fire.mp4` → FireSmoke, "fire"). An original and its converted copies count once; the 16:9 one is suggested. Nothing is linked by this call |
| POST | `/api/analytics/assign` `{video_ids, analytics_ids}` | Operator: add each video to each analytics as a placeholder titled after the file. Existing pairs are skipped (`{created, skipped}`), so it is safe to repeat. 404 lists unknown ids. Library videos list the analytics they are in as `analytics: [{id, name, slot_id}]` |
| GET | `/api/analytics/{id}` | One analytics: its video placeholders (`slots`), each with its video and the streams run from it |
| POST · PATCH · DELETE | `/api/analytics` · `/api/analytics/{id}` | Add (`{name, category_id, description}`), rename/move/describe, delete (409 while it still has placeholders) |
| POST · PATCH · DELETE | `/api/analytics/categories` · `/api/analytics/categories/{id}` | Manage categories (delete: 409 while it still has analytics) |
| POST | `/api/analytics/{id}/slots` `{title, description, video_id?}` | Add a video placeholder |
| PATCH · DELETE | `/api/analytics/slots/{id}` | Edit title/description, attach (`video_id`) or detach (`clear_video`) a video; delete (409 while it has streams) |
| POST | `/api/analytics/{id}/slots/reorder` `{slot_ids}` | Order of the placeholder cards |
| POST | `/api/analytics/slots/{id}/streams` `{count 1–20, name_prefix?, start, end, mode, start_now}` | Create *count* cameras from the placeholder's video, named `<prefix>-1…N` (default prefix: the analytics name). Returns `{created, errors, skipped}`: if capacity runs out midway, the ones created are kept |
| POST | `/api/analytics/{id}/start\|stop\|restart` | Act on every stream of the analytics |
| GET | `/api/analytics/{id}/urls?format=txt\|csv&host=` | All RTSP URLs of the analytics, for the portal (`host` as above) |
| POST | `/api/health/test[?quick=true]` | Run the health test now: `{mode, overall PASS/WARN/FAIL, summary, checks:[{id, name, status PASS/WARN/FAIL/SKIP, detail, ms, data}]}`. The full test reads 2 s from every running stream over loopback (streams waiting for their first viewer are skipped, never woken); `quick` opens no RTSP session |
| GET | `/api/health/test/last` | `{full, quick, auto_interval_s}`: the newest full report and the newest automatic quick check |
| GET · PUT | `/api/settings/capacity` | Stream limits `max_active_streams` (1–500) and `max_transcode_streams` (0–64): value, default, who changed it. PUT `{max_active_streams?, max_transcode_streams?, reset?:[keys]}` (operator); saved in the database, survives restarts |
| GET · PUT | `/api/settings/codecs` | Codecs sent to edges unchanged. PUT (**admin**): H.265 pass-through on/off; applies to streams started or changed afterwards |
| POST | `/api/backup` | Back up the database now (operator); a daily copy is made automatically, 14 kept |
| GET | `/api/metrics` | Prometheus text format (streams by status, per-stream up/viewers/egress/restarts, MediaMTX up, CPU, RAM, disk) |
| GET | `/api/system` | CPU, RAM, disks, network, FFmpeg processes, stream counts and capacity, viewers, warnings, `start_queue` (`spacing_s`, `ready_wait_s`, `phase_margin_s`, `queued`, `launching`) |
| GET | `/api/system/time` | Server clock in ms (`epoch_ms`), for the rig clock in the dashboard and checks against laptops/cameras |
| GET · PUT | `/api/system/encoders` · POST `/api/system/encoders/detect` | Live-transcoding encoders that work here and the one in use; PUT (operator) chooses `auto`/`cpu`/`nvidia`/`intel`/`vaapi`; detect tests them again |
| GET | `/api/system/recovery` | Last boot-recovery report (seconds after OS boot for every stream) |
| GET | `/api/events?limit=&stream_id=&kind=` | Audit log (who did what, failures, recoveries) |
| GET | `/api/mediamtx/status` | State, detection time, last error, recoveries, paths, readers |
| POST | `/api/mediamtx/start` · `/api/mediamtx/restart` | Control MediaMTX (operator) |

## Examples

```bash
H=http://rig02.example.internal:8080; A="-u admin:secret"

# upload (streamed; the server checks disk space before writing)
curl $A --data-binary @clip.mp4 -H "Content-Type: application/octet-stream" "$H/api/videos/upload?filename=clip.mp4"

# find the video id
curl -s $A "$H/api/videos?search=clip" | python3 -m json.tool | grep -E '"(id|filename|compat_mode)"'

# resumable upload (what the dashboard does, in 8 MB chunks)
S=$(curl -s $A -H 'Content-Type: application/json' -X POST $H/api/videos/uploads -d "{\"filename\":\"big.mp4\",\"size\":$(stat -c%s big.mp4)}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s $A -X PUT -H 'Content-Type: application/octet-stream' --data-binary @big.mp4 "$H/api/videos/uploads/$S?offset=0"

# start a whole demo set and export its URLs for the portal
curl -s $A -X POST $H/api/groups/3/start
curl -s $A "$H/api/groups/3/urls" > urls.txt

# create a looping stream from 00:02:00 to 00:07:30 and wait until it is RUNNING
curl -s $A -H 'Content-Type: application/json' -X POST "$H/api/streams?wait=true" \
  -d '{"name":"store3_violation","video_id":4,"start":"00:02:00","end":"00:07:30","mode":"loop"}'

# stop / restart / delete (If-Match optional for scripts)
curl -s $A -X POST "$H/api/streams/12/stop"
curl -s $A -X POST "$H/api/streams/12/restart?wait=true"
curl -s $A -X DELETE "$H/api/streams/12"

# last 20 log lines, health, recovery timings
curl -s $A "$H/api/streams/12/logs?lines=20"
curl -s "$H/api/health?strict=true"
curl -s $A "$H/api/system/recovery"
```

### Stream object (abridged)

```json
{
  "id": 12, "name": "store3_violation", "rtsp_url": "rtsp://rig02.example.internal:8554/store3_violation",
  "source_rel_path": "store3/2026-10-28.mp4", "start_offset_s": 120.0, "effective_start_s": 118.0,
  "end_offset_s": 450.0, "effective_end_s": 450.0, "playback_mode": "loop", "playback_speed": 1.0,
  "output_mode": "copy", "desired_state": "RUNNING", "status": "RUNNING", "pid": 41231,
  "loop_count": 17, "restart_count": 0, "version": 3, "running_since": "2026-11-03T09:12:44+00:00",
  "runtime": {"path_ready": true, "viewer_count": 1, "readers": [{"remote_addr": "10.0.4.31:52944"}],
              "loop_period_s": 332.0, "playback_position_s": 201.4,
              "progress": {"fps": 25.0, "speed": 1.0, "drop_frames": 0}}
}
```
