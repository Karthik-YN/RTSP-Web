import { formatBytes, formatTime, parseTime, suggestName, validateName } from "./format";
import { errorMessage } from "../services/api";

const PATTERN = "^[A-Za-z0-9_-]{1,64}$";

describe("parseTime (mirrors backend rules)", () => {
  it.each([
    ["", null], ["0", 0], ["90", 90], ["01:30", 90], ["1:02:03", 3723], ["01:02:03.250", 3723.25], ["90:00", 5400],
  ])("parses %s", (text, seconds) => {
    expect(parseTime(text as string)).toEqual({ seconds, error: null });
  });
  it.each(["-1", "1:60", "1:61:00", "abc", "1::2", "1:2:3:4"])("rejects %s", (text) => {
    expect(parseTime(text).error).not.toBeNull();
  });
});

describe("formatTime", () => {
  it("formats with and without milliseconds", () => {
    expect(formatTime(3723.25)).toBe("01:02:03.250");
    expect(formatTime(3723.25, false)).toBe("01:02:03");
    expect(formatTime(null)).toBe("–");
  });
});

describe("validateName", () => {
  it("accepts valid names", () => expect(validateName("cam_01-A", PATTERN, [], [])).toBeNull());
  it("rejects spaces, slashes, specials, reserved and duplicates (case-insensitive)", () => {
    expect(validateName("my cam", PATTERN, [], [])).toMatch(/spaces/);
    expect(validateName("a/b", PATTERN, [], [])).toMatch(/slashes/);
    expect(validateName("a#b", PATTERN, [], [])).toMatch(/letters/);
    expect(validateName("", PATTERN, [], [])).toMatch(/required/);
    expect(validateName("ALL", PATTERN, ["all"], [])).toMatch(/reserved/);
    expect(validateName("Cam1", PATTERN, [], ["cam1"])).toMatch(/in use/);
  });
});

describe("suggestName", () => {
  it("derives a valid unique name from a file name", () => {
    expect(suggestName("Store A cam (3).mp4", [])).toBe("Store_A_cam_3");
    expect(suggestName("clip.mp4", ["clip", "clip_2"])).toBe("clip_3");
  });
});

describe("formatBytes", () => {
  it("formats sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(5 * 1024 ** 3)).toBe("5.0 GB");
  });
});

describe("errorMessage", () => {
  it("extracts FastAPI detail strings and objects", () => {
    expect(errorMessage({ detail: "Stream name 'x' is already in use." }, 409).message).toMatch(/already in use/);
    const r = errorMessage({ detail: { message: "Not enough disk space", required: 10, free: 5 } }, 507);
    expect(r.message).toBe("Not enough disk space");
    expect(r.data.required).toBe(10);
    expect(errorMessage(null, 0).message).toMatch(/Cannot reach/);
  });
});
