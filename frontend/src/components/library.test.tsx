import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConversionProgress, FitDialog, VideoPlayer, videoFileUrl } from "./VideoLibrary";
import { blockedShape, OutputSizeField, outputPlan } from "./CreateStreamDialog";
import type { Conversion, Video } from "../types/api";

const video = {
  id: 7, rel_path: "Fire.mp4", filename: "Fire.mp4", size_bytes: 23367491, status: "READY", error: null,
  container: "mp4", duration_s: 28.6, width: 1920, height: 1080, fps: 23.976, fps_text: "24000/1001", is_vfr: false,
  video_codec: "h264", video_profile: "High", pix_fmt: "yuv420p", bit_depth: 8, audio_codec: null, bitrate_bps: 6.5e6,
  keyframe_interval_s: 2, compat_mode: "copy", compat_reasons: [], compat_warnings: [], thumbnail_url: null,
  streams_using: 0, updated_at: "", last_seen_at: "",
} as Video;

describe("Library play and download", () => {
  it("builds the file URLs", () => {
    expect(videoFileUrl(7)).toBe("/api/videos/7/file");
    expect(videoFileUrl(7, true)).toBe("/api/videos/7/file?download=true");
  });

  it("plays the file in the browser and offers a download", () => {
    render(<VideoPlayer v={video} onClose={() => {}} />);
    expect(screen.getByTestId("video-player")).toHaveAttribute("src", "/api/videos/7/file");
    const dl = screen.getByRole("link", { name: /Download/ });
    expect(dl).toHaveAttribute("href", "/api/videos/7/file?download=true");
    expect(dl).toHaveAttribute("download", "Fire.mp4");
  });

  it("does not try to play formats browsers can't play, and offers the H.264 copy", async () => {
    const onConvert = vi.fn();
    const mpeg4 = { ...video, video_codec: "mpeg4", browser_playable: false,
      browser_note: "Browsers can't play MPEG4 video; VLC can." } as Video;
    render(<VideoPlayer v={mpeg4} onClose={() => {}} onConvert={onConvert} />);
    expect(screen.queryByTestId("video-player")).toBeNull();
    expect(screen.getByTestId("not-playable")).toHaveTextContent("Browsers can't play MPEG4 video");
    await userEvent.setup().click(screen.getByRole("button", { name: /Convert to H.264/ }));
    expect(onConvert).toHaveBeenCalled();
    expect(screen.getByRole("link", { name: /Download/ })).toBeInTheDocument();
  });

  it("shows why a conversion could not start inside the player", async () => {
    const mpeg4 = { ...video, video_codec: "mpeg4", browser_playable: false, browser_note: "x" } as Video;
    render(<VideoPlayer v={mpeg4} onClose={() => {}} onConvert={async () => "Not enough disk space for the copy"} />);
    await userEvent.setup().click(screen.getByRole("button", { name: /Convert to H.264/ }));
    expect(await screen.findByText("Not enough disk space for the copy")).toBeInTheDocument();
  });

  it("falls back to the same offer when playback fails anyway", () => {
    render(<VideoPlayer v={video} onClose={() => {}} />);
    fireEvent.error(screen.getByTestId("video-player"));
    expect(screen.getByTestId("not-playable")).toHaveTextContent("can't play this file (h264)");
  });

  it("shows conversion progress with time left", () => {
    const job = { id: "j1", video_id: 7, source: "a.avi", target: "a (h264).mp4", state: "running", progress: 0.42,
      eta_s: 95, error: null, result_video_id: null, username: null, created_at: "", finished_at: null } as Conversion;
    const onCancel = vi.fn();
    render(<ConversionProgress job={job} onCancel={onCancel} />);
    expect(screen.getByTestId("conversion-progress")).toHaveTextContent("Converting to H.264: 42% · ~2 min left");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledWith(job);
  });
});

describe("16:9 and standard output", () => {
  const out = { width: 1280, height: 720, default: "standard" as const, sizes: ["640x360", "1280x720", "1920x1080"], require_16_9: true };
  const wide = { ...video, display_width: 1920, display_height: 1080, shape: "16:9", is_16_9: true } as Video;

  it("says whether a stream runs as-is, from the 720p copy, or scaled live", () => {
    expect(outputPlan(wide, "original", out)).toBe("as-is");
    expect(outputPlan(wide, "standard", out)).toBe("live");
    expect(outputPlan({ ...wide, copy_720p_id: 9 }, "standard", out)).toBe("copy");
    expect(outputPlan({ ...wide, display_width: 1280, display_height: 720 }, "standard", out)).toBe("as-is");
    expect(blockedShape({ ...wide, is_16_9: false }, out)).toBe(true);
    expect(blockedShape({ ...wide, is_16_9: false }, { ...out, require_16_9: false })).toBe(false);
  });

  it("offers Standard and Original, and other sizes only to admins", async () => {
    const onChange = vi.fn();
    const { rerender } = render(<OutputSizeField out={out} video={wide} value="standard" onChange={onChange} isAdmin={false} idPrefix="t" />);
    expect(screen.getByTestId("output-plan")).toHaveTextContent("Scaled live");
    expect(screen.queryByLabelText("Other output size")).toBeNull();
    await userEvent.setup().click(screen.getByLabelText(/Original \(1920×1080\)/));
    expect(onChange).toHaveBeenCalledWith("original");
    rerender(<OutputSizeField out={out} video={{ ...wide, copy_720p_id: 3 }} value="standard" onChange={onChange} isAdmin idPrefix="t" />);
    expect(screen.getByTestId("output-plan")).toHaveTextContent("720p copy");
    expect(screen.getByLabelText("Other output size")).toBeInTheDocument();
  });

  it("asks how to fit a portrait video into 16:9 and starts the copy", async () => {
    const onStart = vi.fn(async () => null);
    const portrait = { ...video, filename: "phone.mp4", display_width: 1080, display_height: 1920, shape: "portrait", is_16_9: false } as Video;
    render(<FitDialog v={portrait} out={out} onClose={() => {}} onStart={onStart} />);
    expect(screen.getByText(/1080×1920 \(portrait\)/)).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByLabelText(/Crop to fill/));
    await user.click(screen.getByRole("button", { name: /Make 16:9 copy/ }));
    expect(onStart).toHaveBeenCalledWith("crop");
  });
});
