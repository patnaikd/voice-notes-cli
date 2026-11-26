import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { createStore } from "../state/store";
import { line, meeting, seg, snapshot } from "./fixtures";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/devices") return jsonResponse({ microphones: [{ name: "Built-in", uid: "b", default: true }], outputs: [] });
    if (url === "/api/meetings/merge") return jsonResponse({ detail: "Meetings overlap" }, 409);
    return jsonResponse({ detail: "not found" }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderWith(over = {}) {
  const store = createStore();
  render(<App store={store} connect={false} />);
  act(() => {
    store.dispatch({ type: "snapshot", ...snapshot(over) });
    store.dispatch({ type: "conn", status: "live" });
  });
  return store;
}

describe("App", () => {
  it("renders the recorder bar, live lines and meeting cards from a snapshot", async () => {
    const store = renderWith({
      recorder: {
        ...snapshot().recorder,
        state: "recording",
        segment_id: "s2",
        segment_started_at: "2026-11-25T15:00:00-08:00",
        next_boundary_at: "2026-11-25T15:30:00-08:00",
      },
      segments: [
        seg("s2", { status: "recording", ended_at: null, meeting_id: "m1", started_at: "2026-11-25T15:00:00-08:00" }),
        seg("s1", {
          meeting_id: "m1",
          continuation: null,
        }),
      ],
      meetings: [meeting("m1", { segment_ids: ["s1", "s2"], merged_by: "auto", auto_merge_reason: "Same topic" })],
      live: { segment_id: "s2", lines: [line("a", { segment_id: "s2", speaker: "Alice", text: "Hello there" })] },
    });

    expect(screen.getByTestId("recorder-state").textContent).toBe("Recording");
    expect(screen.getByTestId("conn-status").dataset.status).toBe("live");
    expect((screen.getByTestId("start-btn") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("stop-btn") as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByTestId("split-btn") as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByTestId("level-mic")).toBeTruthy();
    expect(screen.getByTestId("level-system")).toBeTruthy();
    expect(screen.getAllByTestId("live-line")).toHaveLength(1);
    expect(screen.getByText("Hello there")).toBeTruthy();

    const card = screen.getByTestId("meeting-card");
    expect(within(card).getAllByTestId("segment-chip")).toHaveLength(2);
    expect(within(card).getByTestId("split-meeting-btn")).toBeTruthy();
    expect(within(card).getByTestId("notes-status").textContent).toBe("Notes ready");

    act(() => store.dispatch({ type: "partial", source: "mic", text: "typing", speaker: null }));
    expect(screen.getByTestId("partial-mic").textContent).toContain("typing");
    act(() =>
      store.dispatch({ type: "line", line: line("a", { segment_id: "s2", speaker: "Alice", text: "Hello there, revised" }) }),
    );
    expect(screen.getByTestId("partial-mic").textContent).toBe("");
    expect(screen.getAllByTestId("live-line")).toHaveLength(1);
    expect(screen.getByText("Hello there, revised")).toBeTruthy();

    act(() => store.dispatch({ type: "meeting_removed", id: "m1" }));
    // Segments still reference m1, so a placeholder card remains until the server sends the new meeting.
    expect(screen.getByTestId("meeting-card").textContent).toContain("Loading…");
  });

  it("enables merge with two selections and surfaces API errors as a toast", async () => {
    renderWith({
      segments: [seg("s1", { meeting_id: "m1" }), seg("s2", { meeting_id: "m2", started_at: "2026-11-25T15:00:00-08:00" })],
      meetings: [meeting("m1", { segment_ids: ["s1"] }), meeting("m2", { segment_ids: ["s2"], started_at: "2026-11-25T15:00:00-08:00" })],
    });
    const merge = screen.getByTestId("merge-btn") as HTMLButtonElement;
    expect(merge.disabled).toBe(true);
    const boxes = screen.getAllByTestId("meeting-select");
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[1]);
    expect(merge.disabled).toBe(false);
    await act(async () => {
      fireEvent.click(merge);
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/meetings/merge", expect.objectContaining({ method: "POST" }));
    expect((await screen.findByTestId("toast")).textContent).toContain("Meetings overlap");
  });

  it("shows health warnings and the reconnecting state", () => {
    const store = renderWith({
      health: { ...snapshot().health, warnings: ["Server memory above 1024 MB"], errors_last_hour: 2 },
    });
    const panel = screen.getByTestId("health-panel");
    expect(within(panel).getByText("Server memory above 1024 MB")).toBeTruthy();
    act(() => store.dispatch({ type: "conn", status: "reconnecting" }));
    expect(screen.getByTestId("conn-status").textContent).toContain("Reconnecting");
  });
  it("loads older segments with the meetings the server returns alongside them", async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/segments?before=s2"))
        return jsonResponse({ segments: [seg("s1", { meeting_id: "m1" })], meetings: [meeting("m1", { title: "Older one", segment_ids: ["s1"] })] });
      return jsonResponse({ detail: "not found" }, 404);
    });
    // A full snapshot page (200 segments) means older ones may exist.
    const recent = Array.from({ length: 199 }, (_, i) =>
      seg(`r${i}`, { meeting_id: "m2", started_at: `2026-11-26T${String(Math.floor(i / 60)).padStart(2, "0")}:${String(i % 60).padStart(2, "0")}:00-08:00` }),
    );
    renderWith({
      segments: [...recent, seg("s2", { meeting_id: "m2", started_at: "2026-11-25T15:00:00-08:00" })],
      meetings: [meeting("m2", { segment_ids: ["s2", ...recent.map((r) => r.id)], started_at: "2026-11-25T15:00:00-08:00" })],
    });
    await act(async () => {
      fireEvent.click(screen.getByText("Load older"));
    });
    expect(await screen.findByText("Older one")).toBeTruthy();
    expect(screen.getAllByTestId("meeting-card")).toHaveLength(2);
    expect(screen.getByText("No older segments.")).toBeTruthy();
  });

  it("relabels live lines when a speaker is named", () => {
    const uuid = "3f2a91c4-0000-4000-8000-000000000001";
    const store = renderWith({
      live: { segment_id: "s1", lines: [line("a", { source: "system", speaker: "Unknown speaker 3f2a91c4", speaker_uuid: uuid })] },
    });
    expect(screen.getByTestId("live-line").textContent).toContain("Unknown speaker 3f2a91c4");
    act(() =>
      store.dispatch({
        type: "speakers",
        speakers: [{ id: uuid, name: "Alice", display_name: "Alice", sample_seconds: 3, created_at: "2026-11-25T10:00:00-08:00", has_sample: true }],
      }),
    );
    expect(screen.getByTestId("live-line").textContent).toContain("Alice");
  });
});
