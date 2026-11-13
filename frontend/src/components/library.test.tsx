import { fireEvent, render, screen } from "@testing-library/react";
import { VideoPlayer, videoFileUrl } from "./VideoLibrary";
import type { Video } from "../types/api";

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

  it("explains when the browser cannot play the codec", () => {
    render(<VideoPlayer v={{ ...video, video_codec: "hevc" }} onClose={() => {}} />);
    expect(screen.getByText(/may not play in every browser/)).toBeInTheDocument();
    fireEvent.error(screen.getByTestId("video-player"));
    expect(screen.getByText(/can't play this file \(hevc\)/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Download/ })).toBeInTheDocument();
  });
});
