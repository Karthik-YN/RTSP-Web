import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describeLeadIn, leadInBody, LeadInFields, leadText, type LeadInValue } from "./LeadIn";
import { isProblem } from "./StreamsTable";
import type { Stream } from "../types/api";

const stream = (lead: unknown, status = "WAITING_FOR_VIEWER") =>
  ({ status, desired_state: "RUNNING", runtime: { start_queue: null, lead_in: lead } }) as unknown as Stream;

test("lead-in status texts", () => {
  expect(leadText(stream({ phase: "waiting", lead_in_s: 5 }))).toBe("Waiting for a viewer · video 5 s after one connects");
  expect(leadText(stream({ phase: "countdown", video_in_s: 2.2, viewer_seen_at: null }, "LEAD_IN"))).toBe("Lead-in · video in 3 s");
  expect(leadText(stream({ phase: "countdown", video_in_s: 0.4, viewer_seen_at: "x", viewer: "edge-1" }, "LEAD_IN")))
    .toBe("Viewer connected (edge-1) · video in 1 s");
  expect(leadText(stream(null, "RUNNING"))).toBeNull();
});

test("a stream in its lead-in is not a problem", () => {
  expect(isProblem(stream({ phase: "waiting", lead_in_s: 5 }))).toBe(false);
  expect(isProblem(stream(null, "LEAD_IN"))).toBe(false);
});

test("describe and body", () => {
  expect(describeLeadIn({ lead_in_mode: "hold", lead_in_s: 5, hold_max_s: 600 })).toContain("until a viewer connects");
  expect(leadInBody({ mode: "fixed", seconds: 5, holdMax: 30 })).toEqual({ lead_in_mode: "fixed", lead_in_s: 5, hold_max_s: null });
});

function Harness({ onValue }: { onValue: (v: LeadInValue) => void }) {
  const [v, setV] = useState<LeadInValue>({ mode: "fixed", seconds: 5, holdMax: null });
  return <LeadInFields value={v} onChange={(n) => { setV(n); onValue(n); }} />;
}

test("choosing hold and a timeout", async () => {
  let last: LeadInValue | null = null;
  render(<Harness onValue={(v) => { last = v; }} />);
  await userEvent.click(screen.getByLabelText("Until a viewer connects"));
  expect(last!.mode).toBe("hold");
  await userEvent.type(screen.getByPlaceholderText("never"), "600");
  expect(last!.holdMax).toBe(600);
  const secs = screen.getByLabelText("Video starts");
  await userEvent.clear(secs);
  await userEvent.type(secs, "500");
  expect(screen.getByText("Seconds must be between 0 and 120.")).toBeInTheDocument();
});
