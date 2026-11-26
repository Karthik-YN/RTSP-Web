import { describe, expect, it } from "vitest";
import { closeClip, totalOf } from "./ClipperPage";
import { clipperHref } from "../components/CreateStreamDialog";

describe("clipper", () => {
  it("closes a clip in either direction and ignores tiny ones", () => {
    expect(closeClip([], 10, 14)).toEqual([[10, 14]]);
    expect(closeClip([[1, 2]], 14, 10)).toEqual([[1, 2], [10, 14]]); // stopped after seeking back
    const segs: [number, number][] = [[1, 2]];
    expect(closeClip(segs, 5, 5.1)).toBe(segs);
    expect(totalOf([[1, 2], [10, 14.5]])).toBeCloseTo(5.5);
  });
  it("links the dialog's times into the clipper", () => {
    expect(clipperHref(7, null, null)).toBe("/clip/7");
    expect(clipperHref(7, 12.5, 90)).toBe("/clip/7?start=12.5&end=90");
    expect(clipperHref(7, 3, null)).toBe("/clip/7?start=3");
    expect(clipperHref(7, null, null, "http://h:8083")).toBe("http://h:8083/clip/7");
  });
});
