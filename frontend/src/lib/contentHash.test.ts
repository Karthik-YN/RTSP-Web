import { findSameContent, quickHash, sha256Hex } from "./contentHash";
import type { Video } from "../types/api";

const hex = (s: string) => sha256Hex(new TextEncoder().encode(s));

describe("content fingerprint (same as the server's quick_hash)", () => {
  it("SHA-256 matches known values", () => {
    expect(hex("")).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    expect(hex("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    expect(hex("a".repeat(1000))).toBe("41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3");
  });

  it("hashes size + first MiB (+ last MiB above 2 MiB), like file_manager.quick_hash", async () => {
    const small = new Uint8Array(10).fill(7);
    expect(await quickHash(new Blob([small]))).toBe(sha256Hex(new Uint8Array([...new TextEncoder().encode("10"), ...small])));
    const MiB = 1024 * 1024;
    const big = new Uint8Array(2 * MiB + 5).map((_, i) => i % 256);
    const expect_ = sha256Hex(new Uint8Array([...new TextEncoder().encode(String(big.length)), ...big.slice(0, MiB), ...big.slice(big.length - MiB)]));
    expect(await quickHash(new Blob([big]))).toBe(expect_);
  });

  it("finds a library video with the same content, not the same name", () => {
    const h = "ab12cd34" + "0".repeat(56);
    const lib = [
      { id: 1, filename: "x.mp4", size_bytes: 10, quick_hash: h },
      { id: 2, filename: "y.mp4", size_bytes: 20, thumbnail_url: "/api/videos/2/thumbnail?h=ff00ff00" },
    ] as unknown as Video[];
    expect(findSameContent(10, h, lib)?.id).toBe(1);
    expect(findSameContent(11, h, lib)).toBeNull(); // size differs
    expect(findSameContent(20, "ff00ff00" + "1".repeat(56), lib)?.id).toBe(2); // older server: hash prefix in thumbnail URL
    expect(findSameContent(20, "ee00ff00" + "1".repeat(56), lib)).toBeNull();
  });
});
