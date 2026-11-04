# Troubleshooting

Start with `scripts/healthcheck.sh --rtsp` on the server and the **Events** tab in the dashboard. Every failure, recovery and user action is logged there with who and when.

| Symptom | Likely cause | What to do |
|---|---|---|
| Dashboard unreachable | `rtsp-manager` not running, firewall, wrong address | `systemctl status rtsp-manager`; `journalctl -u rtsp-manager -n 100`; `curl http://127.0.0.1:8080/api/health` on the server; check ufw for 8080 |
| Backend exits at start: "Another rtsp-manager backend is already running" | A second instance (e.g. a manual uvicorn) holds `/var/lib/rtsp-manager/backend.lock` | Stop the other instance; never run more than one worker |
| Red banner "MediaMTX is down" that does not clear | MediaMTX cannot start (config error, port in use) | `journalctl -u mediamtx -n 50`. "address already in use" → another process holds 8554/9997 (`ss -ltnp`); "configuration" errors → `mediamtx /etc/mediamtx/mediamtx.yml` in a terminal shows the line |
| "recovery failed: systemctl start failed … Interactive authentication required" | polkit rule missing or polkit not installed | Check `/etc/polkit-1/rules.d/50-rtsp-manager.rules` and that `polkitd` is installed; systemd's own restart still works |
| Stream stuck in **STARTING** then RESTARTING | FFmpeg cannot publish (MediaMTX rejecting, bad file) | Open the stream → last 20 log lines. "Connection refused" → MediaMTX down. "400 Bad Request"/"already publishing" → another publisher on the path (`overridePublisher: no`); find it in `curl 127.0.0.1:9997/v3/paths/list` |
| Stream in **ERROR** "Gave up after 10 consecutive failures" | Persistent FFmpeg failure | Read the log tail and Last error; fix the cause, then **Restart** |
| Stream in **ERROR** "Source file was deleted / replaced / modified" | The file changed while streaming (EC-07) | Intended. Restore the file or press **Restart** to re-validate against the current file |
| "There is no keyframe between the start and end times" | Segment shorter than the GOP | Choose a later end; the library shows the keyframe interval |
| Start time is earlier than what I typed | Keyframe alignment (FFMPEG.md §5) | Expected: the effective start is shown. Re-encode the source with shorter GOPs if second-exact starts matter |
| Edge shows a black or grey picture for a few seconds after connecting | The reader joined mid-GOP and waits for the next keyframe | Expected up to one GOP; long-GOP files are flagged in the library |
| Edge reports "no stream" before publishing starts | Edge gave up after the 60 s early-reader hold, or does not retry | Start the stream, then re-enable the camera; record the behaviour in EDGE_VALIDATION E-12; raise `runOnDemandStartTimeout` if needed |
| Video plays too fast or too slow | Should not happen (`-re`, timestamps preserved) | Run `scripts/validate_rtsp.sh -t 60 <url>`: ratio must be about 1.0. Check the stream detail "speed" (≈1.0x). Report with the log |
| Transcoded stream stutters, speed < 1.0x | CPU saturated | Check the CPU meter and per-stream CPU; lower `RTSPM_MAX_TRANSCODE_STREAMS` or `RTSPM_X264_PRESET=ultrafast`; prefer H.264 sources |
| Upload refused "Not enough disk space" | Free space minus the reserve (default max(5 GiB, 5 %)) is smaller than the file, counting other uploads in progress (EC-08) | Free space; the error shows required/free/available. Adjust `RTSPM_DISK_RESERVE_*` only if you understand the risk |
| Upload fails at a few hundred MB behind nginx | nginx buffering or body limit | Use `deployment/nginx.conf` (`client_max_body_size 64g; proxy_request_buffering off`) |
| A copied-in file does not appear | Still being written (settle window) or unsupported extension, hidden name, symlink | Wait about 30 s and Refresh; check the extension list in `/api/config`; symlinks are ignored on purpose |
| "Copy" button shows a prompt instead of copying | The page is served over plain HTTP (no clipboard API) | Copy from the prompt (Ctrl+C), or serve the dashboard over HTTPS |
| Viewer count shows "?" | MediaMTX is down; reader info comes from its API | Fix MediaMTX |
| Long-running loop reconnects once after ~7 days | The loop playlist (7 days) ran out and a fresh publisher started | Expected (ARCHITECTURE §11); raise `RTSPM_LOOP_PLAYLIST_HOURS` |

| "Too many failed logins. Try again in N s" (429) | 5 wrong passwords for that user (or 30 from one address) | Wait; the lockout doubles per further failure (max 15 min). An admin can reset the password on the Users page |
| "Stream 'x' is protected by alice" (403) | The owner protected it (see the note on the card) | Ask the owner, or an admin, to unprotect it or transfer ownership |
| `systemctl stop rtsp-manager` but the cameras keep playing | By design: publishers survive backend restarts and upgrades | Stop streams in the UI, or `sudo -u rtspmgr /opt/rtsp-manager/scripts/stop_publishers.sh` |
| After an upgrade one stream reconnected once | Its publish command changed in the new version, so it was replaced rather than adopted | Expected; all other streams were re-adopted without interruption |
| No alert messages arrive | `RTSPM_ALERT_WEBHOOK_URL` unset or wrong; alerts are rate-limited (5 min per kind+stream) | `journalctl -u rtsp-manager | grep "alert webhook failed"` |
| Upload says "connection lost, retrying" | Wi-Fi dropped; the upload resumes from the last received byte | Nothing to do; after ~2 min of failures it gives up and keeps nothing |
| `install.sh --upgrade-mediamtx` says "exposes extra ports" and restored the previous binary | The new MediaMTX enables a new server by default | Add `<server>: no` for it to `/etc/mediamtx/mediamtx.yml`, then retry |

## Useful commands

```bash
curl -s 127.0.0.1:9997/v3/paths/list | python3 -m json.tool        # what MediaMTX is serving (server only)
curl -s 127.0.0.1:9997/v3/rtspsessions/list | python3 -m json.tool # who is connected
ps -o pid,etime,pcpu,rss,args -C ffmpeg                             # publishers
sudo -u rtspmgr sqlite3 /var/lib/rtsp-manager/rtsp-manager.db \
  "select id,name,desired_state,status,last_error from streams"
ffprobe -v error -rtsp_transport tcp -show_streams rtsp://127.0.0.1:8554/<name>
```
