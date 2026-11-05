# RIG-02 REST API

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

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health[?strict=true]` | Liveness: `{status: ok|degraded, mediamtx, recovery, version}` (no auth) |
| POST | `/api/auth/login` · `/api/auth/logout` · GET `/api/auth/me` | Session handling |
| POST | `/api/auth/password` `{current_password,new_password}` | Change your own password |
| GET/POST | `/api/users` · PATCH/DELETE `/api/users/{name}` | **Admin**: list/create users, change role or reset password, delete (the last admin is protected) |
| GET | `/api/config` | Public RTSP host/port, URL template, name rules, limits, output profile, warnings |
| GET | `/api/videos?search=&status=&include_missing=` | Library with FFprobe metadata and compatibility verdict |
| POST | `/api/videos/refresh` | Rescan the server folder now |
| POST | `/api/videos/upload/check` `{filename,size}` | Pre-flight: name and disk-space check, no data sent |
| POST | `/api/videos/upload?filename=` (raw body) | Streamed single-request upload (`Content-Length` required) |
| POST | `/api/videos/uploads` `{filename,size}` | Start a **resumable** upload: reserves disk space, returns `{id, offset}` |
| PUT | `/api/videos/uploads/{id}?offset=N` (raw body) | Append bytes; 200 `{offset}` while incomplete, 201 + video when done, 409 `{offset}` on a wrong offset |
| GET/DELETE | `/api/videos/uploads/{id}` | Ask the current offset (after a dropped connection) / abort and release the space |
| DELETE | `/api/videos/{id}` | **Admin**: move to `.trash` (purged after 7 days); 409 while any stream uses it |
| GET | `/api/videos/{id}` · `/thumbnail` · `/frame?t=` | Metadata, thumbnail, frame at time *t* (JPEG) |
| GET | `/api/streams` | All streams with live runtime (status, path ready, viewers, loops, progress) |
| POST | `/api/streams/validate` | Dry run of a create: effective (keyframe) start/end, output mode, warnings |
| POST | `/api/streams[?wait=true]` | Create (and start unless `start_now:false`) |
| GET | `/api/streams/{id}` | Detail: command, PID, config, source metadata, last 20 log lines, events |
| PATCH | `/api/streams/{id}` | Edit (`If-Match`). Config changes restart a running stream; `note`, `protected` (owner/admin) and `owner` (admin) do not. Renaming requires a stopped stream |
| POST | `/api/streams/{id}/start|stop|restart[?wait=true]` | Lifecycle |
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
| GET | `/api/groups/{id}/urls?format=txt\|csv` | All RTSP URLs of a group (for the portal or `measure_recovery.py`) |
| GET | `/api/metrics` | Prometheus text format (streams by status, per-stream up/viewers/egress/restarts, MediaMTX up, CPU, RAM, disk) |
| GET | `/api/system` | CPU, RAM, disks, network, FFmpeg processes, stream counts and capacity, viewers, warnings |
| GET | `/api/system/recovery` | Last boot-recovery report (seconds after OS boot for every stream) |
| GET | `/api/events?limit=&stream_id=&kind=` | Audit log (who did what, failures, recoveries) |
| GET | `/api/mediamtx/status` | State, detection time, last error, recoveries, paths, readers |
| POST | `/api/mediamtx/start` · `/api/mediamtx/restart` | Control MediaMTX (operator) |

## Examples

```bash
H=http://rig02.example.internal:8080; A="-u admin:secret"

# upload (streamed; the server checks disk space before writing)
curl $A -T clip.mp4 "$H/api/videos/upload?filename=clip.mp4"

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
