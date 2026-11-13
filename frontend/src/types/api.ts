// Mirrors backend/app/schemas (see /docs on the server for the OpenAPI spec).

export type StreamStatus =
  | "STOPPED" | "STARTING" | "RUNNING" | "STOPPING" | "RESTARTING" | "ERROR" | "COMPLETED"
  | "LEAD_IN" | "WAITING_FOR_VIEWER";

export type LeadInMode = "none" | "fixed" | "hold";

export interface LeadInState {
  mode: LeadInMode;
  phase: "preparing" | "waiting" | "countdown";
  lead_in_s: number | null;
  hold_max_s: number | null;
  video_in_s: number | null;
  slate_at: string | null;
  viewer_seen_at: string | null;
  viewer: string | null;
  slate_style: string | null;
}

export interface Reader {
  type: string | null;
  id: string | null;
  remote_addr: string | null;
  hostname?: string | null;
  since: string | null;
  bytes_sent: number | null;
  transport: string | null;
}

export interface Progress {
  frame: number;
  fps: number;
  out_time_s: number;
  speed: number | null;
  drop_frames: number;
  dup_frames: number;
  total_size: number;
  bitrate: string | null;
  updated_at: string | null;
}

export interface StreamRuntime {
  path_ready: boolean;
  path_known: boolean;
  readers: Reader[];
  viewer_count: number;
  viewer_peak_today: number;
  preview_watchers: number;
  bytes_received: number | null;
  ingest_bps: number | null;
  egress_bps: number | null;
  tracks: string[];
  progress: Progress | null;
  process: { cpu_percent: number; rss_bytes: number } | null;
  loop_count: number;
  loop_period_s: number | null;
  playback_position_s: number | null;
  waiting_for_mediamtx: boolean;
  consecutive_failures: number;
  suppressed_timestamp_warnings: number;
  /** Waiting in the start queue (2 s spacing between camera starts). */
  start_queue: { position: number; eta_s: number | null } | null;
  /** Set when the clip is too short to keep all cameras of this video apart. */
  spacing_warning: string | null;
  /** Set while the stream's slate is on its URL and the video has not started. */
  lead_in?: LeadInState | null;
  slate_on_url?: boolean;
}

export interface Stream {
  id: number;
  name: string;
  rtsp_url: string;
  video_id: number | null;
  source_rel_path: string;
  start_offset_s: number;
  effective_start_s: number;
  end_offset_s: number | null;
  effective_end_s: number | null;
  playback_mode: "loop" | "once";
  playback_speed: number;
  force_transcode: boolean;
  output_mode: "copy" | "transcode" | null;
  desired_state: "RUNNING" | "STOPPED";
  status: StreamStatus;
  lead_in_mode: LeadInMode;
  lead_in_s: number;
  hold_max_s: number | null;
  status_detail: string | null;
  pid: number | null;
  last_error: string | null;
  loop_count: number;
  restart_count: number;
  version: number;
  created_at: string;
  created_by: string | null;
  updated_at: string;
  updated_by: string | null;
  last_start_at: string | null;
  last_stop_at: string | null;
  running_since: string | null;
  owner: string | null;
  note: string | null;
  protected: boolean;
  group_id: number | null;
  slot_id: number | null;
  warnings: string[];
  runtime: StreamRuntime;
}

export interface Video {
  id: number;
  rel_path: string;
  filename: string;
  size_bytes: number;
  status: "READY" | "INVALID" | "MISSING";
  error: string | null;
  container: string | null;
  duration_s: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  fps_text: string | null;
  is_vfr: boolean;
  video_codec: string | null;
  video_profile: string | null;
  pix_fmt: string | null;
  bit_depth: number | null;
  audio_codec: string | null;
  bitrate_bps: number | null;
  keyframe_interval_s: number | null;
  compat_mode: "copy" | "transcode" | "reject" | null;
  compat_reasons: string[];
  compat_warnings: string[];
  /** Plays in current browsers (false: MPEG-4 Part 2, H.265, 10-bit, MKV/AVI…). */
  browser_playable?: boolean;
  browser_note?: string | null;
  /** Why "Convert to H.264" would help; empty when it would not. */
  convert_reasons?: string[];
  converting?: Conversion | null;
  thumbnail_url: string | null;
  streams_using: number;
  updated_at: string;
  last_seen_at: string;
}

export interface StreamDetail extends Stream {
  ffmpeg_command: string[] | null;
  ffmpeg_command_text: string | null;
  config: Record<string, unknown>;
  video: Video | null;
  log_tail: string[];
  events: { ts: string; level: string; kind: string; user: string | null; message: string }[];
}

export interface ValidateResult {
  ok: boolean;
  errors: { field: string | null; message: string }[];
  start_s: number | null;
  effective_start_s: number | null;
  end_s: number | null;
  effective_end_s: number | null;
  period_s: number | null;
  output_mode: string | null;
  warnings: string[];
  rtsp_url: string | null;
}

export interface DiskInfo {
  path: string;
  total: number;
  used: number;
  free: number;
  percent: number | null;
}

export interface SystemInfo {
  cpu_percent: number;
  cpu_count: number;
  load_avg: number[] | null;
  memory: { total: number; available: number; used: number; percent: number };
  disks: Record<string, DiskInfo>;
  ffmpeg_processes: number;
  ffmpeg_cpu_percent: number;
  ffmpeg_rss_bytes: number;
  net_sent_bps?: number;
  net_recv_bps?: number;
  streams: {
    total: number;
    desired_running: number;
    running: number;
    errors: number;
    error_names: string[];
    transcoding: number;
    max_active: number;
    max_transcode: number;
    capacity_percent: number | null;
  };
  viewers: number | null;
  start_queue?: { spacing_s: number; ready_wait_s: number; phase_margin_s: number; queued: number; launching: boolean };
  rtsp_ingest_bps?: number | null;
  rtsp_egress_bps?: number | null;
  warnings: string[];
}

export interface MediaMTXStatus {
  state: "UP" | "DOWN" | "UNKNOWN";
  control_mode: string;
  auto_recover: boolean;
  consecutive_failures: number;
  last_ok_at: string | null;
  down_since: string | null;
  last_detection_s: number | null;
  last_error: string | null;
  recoveries: number;
  last_recovery: { recovered_at: string; outage_s: number | null } | null;
  last_control_message: string | null;
  path_count: number;
  ready_paths: number;
  total_readers: number;
}

export type CheckStatus = "PASS" | "WARN" | "FAIL" | "SKIP";

export interface HealthCheck {
  id: string;
  name: string;
  status: CheckStatus;
  detail: string;
  ms: number;
  data: Record<string, unknown>;
}

export interface HealthReport {
  mode: "full" | "quick";
  overall: "PASS" | "WARN" | "FAIL";
  started_at: string;
  duration_s: number;
  server: string;
  summary: Record<CheckStatus, number>;
  checks: HealthCheck[];
}

export interface LimitInfo {
  value: number;
  default: number;
  overridden: boolean;
  updated_at: string | null;
  updated_by: string | null;
  min: number;
  max: number;
}

export interface Capacity {
  max_active_streams: LimitInfo;
  max_transcode_streams: LimitInfo;
}

export interface RtspHost {
  host: string;
  kind: "canonical" | "alternate" | "browser";
  /** "tailscale": reachable from any network by devices on the tailnet. */
  network?: "tailscale" | null;
}

export interface AppConfig {
  version: string;
  public_rtsp_host: string;
  public_rtsp_port: number;
  rtsp_hosts?: RtspHost[];
  server_addresses?: string[];
  rtsp_url_template: string;
  stream_name_pattern: string;
  reserved_names: string[];
  max_active_streams: number;
  transcode_policy: string;
  min_segment_s: number;
  allowed_extensions: string[];
  upload_max_bytes: number;
  auth_enabled: boolean;
  ffmpeg_version: string | null;
  warnings: string[];
  default_lead_in_mode?: LeadInMode;
  default_lead_in_s?: number;
}

export interface User {
  username: string;
  role: "admin" | "operator" | "viewer";
  can_write: boolean;
  is_admin: boolean;
  auth_enabled: boolean;
}

export interface AppEvent {
  id: number;
  ts: string;
  level: string;
  kind: string;
  stream_id: number | null;
  stream_name: string | null;
  username: string | null;
  message: string;
}

export interface UploadCheck {
  filename: string;
  exists: boolean;
  ok: boolean;
  required: number;
  free: number;
  reserve: number;
  in_flight_uploads: number;
  available: number;
}

export interface StreamHistory {
  hours: number;
  since: string;
  segments: { status: StreamStatus; start: string; end: string; seconds: number }[];
  running_percent: number;
  changes: number;
}

export interface StreamGroup {
  id: number;
  name: string;
  description: string | null;
  created_by: string | null;
  created_at: string;
  stream_ids: number[];
  running: number;
  total: number;
}

export interface BulkResult {
  ok: number;
  failed: number;
  results: { id: number; name?: string; ok: boolean; result?: string; error?: string }[];
}

export interface AnalyticsCounts {
  streams: number;
  running: number;
  problems: number;
  viewers: number;
}

export interface AnalyticsItem extends AnalyticsCounts {
  id: number;
  name: string;
  description: string | null;
  sort: number;
  slots: number;
  slot_ids: number[];
}

export interface AnalyticsCategory {
  id: number;
  name: string;
  sort: number;
  analytics: AnalyticsItem[];
}

export interface AnalyticsSlot {
  id: number;
  analytics_id: number;
  title: string;
  description: string | null;
  sort: number;
  video: Video | null;
  streams: Stream[];
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface AnalyticsDetail extends AnalyticsCounts {
  id: number;
  name: string;
  description: string | null;
  category: { id: number; name: string };
  slots: AnalyticsSlot[];
}

export interface RunResult {
  created: Stream[];
  errors: { name: string; error: string }[];
  skipped: string[];
}

export interface Conversion {
  id: string;
  video_id: number;
  source: string;
  target: string;
  state: "queued" | "running" | "done" | "failed" | "cancelled";
  progress: number;
  eta_s: number | null;
  error: string | null;
  result_video_id: number | null;
  username: string | null;
  created_at: string;
  finished_at: string | null;
}
