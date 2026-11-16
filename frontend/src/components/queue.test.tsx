import { isProblem, queueText } from "./StreamsTable";
import type { Stream } from "../types/api";

const base = { status: "STARTING", desired_state: "RUNNING", runtime: { start_queue: null, spacing_warning: null } } as unknown as Stream;

test("queued streams show their place and are not problems", () => {
  const queued = { ...base, runtime: { ...base.runtime, start_queue: { position: 3, eta_s: 4.2 } } } as Stream;
  expect(queueText(queued)).toBe("Queued #3 · starts in ~5 s");
  expect(isProblem(queued)).toBe(false);
  const next = { ...base, runtime: { ...base.runtime, start_queue: { position: 1, eta_s: 0 } } } as Stream;
  expect(queueText(next)).toBe("Next to start · starts in ~1 s");
});

test("starting is on track; a start that timed out or crashed (RESTARTING) is a problem", () => {
  expect(queueText(base)).toBeNull();
  expect(isProblem(base)).toBe(false); // bounded by startup_timeout_s, then RESTARTING
  expect(isProblem({ ...base, status: "RESTARTING" } as Stream)).toBe(true);
  expect(isProblem({ ...base, status: "STOPPED" } as Stream)).toBe(true); // should run but does not
  expect(isProblem({ ...base, status: "ERROR", desired_state: "STOPPED" } as Stream)).toBe(true);
  expect(isProblem({ ...base, status: "STOPPED", desired_state: "STOPPED" } as Stream)).toBe(false);
});
