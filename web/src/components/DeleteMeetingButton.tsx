import { useState } from "react";
import { api } from "../api/client";
import { useDispatch } from "../state/store";
import type { Meeting } from "../types";
import { ConfirmDialog } from "./ConfirmDialog";

/** Delete button with a confirmation. The server moves the meeting's folders to trash/. */
export function DeleteMeetingButton({
  meeting,
  disabled,
  onDeleted,
}: {
  meeting: Meeting;
  disabled?: boolean;
  onDeleted?: () => void;
}) {
  const dispatch = useDispatch();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const remove = async () => {
    setConfirming(false);
    setBusy(true);
    try {
      await api.deleteMeeting(meeting.id);
      // The server broadcasts these too; applying them now keeps the UI snappy.
      dispatch({ type: "meeting_removed", id: meeting.id });
      for (const id of meeting.segment_ids) dispatch({ type: "segment_removed", id });
      dispatch({ type: "toast", level: "info", message: `Deleted “${meeting.title}”` });
      onDeleted?.();
    } catch {
      /* toast */
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button
        className="btn btn-ghost btn-sm btn-danger-text"
        data-testid="delete-meeting-btn"
        onClick={() => setConfirming(true)}
        disabled={disabled || busy}
        title={disabled ? "Stop recording before deleting this meeting" : "Delete this meeting"}
      >
        {busy ? "Deleting…" : "Delete"}
      </button>
      {confirming && (
        <ConfirmDialog
          title={`Delete “${meeting.title}”?`}
          body={
            <>
              Its transcript, notes and {meeting.segment_ids.length === 1 ? "segment" : `${meeting.segment_ids.length} segments`}{" "}
              disappear from the app. The files are moved to the <code>trash</code> folder in the data directory, so they can
              be restored by hand.
            </>
          }
          confirmLabel="Delete meeting"
          danger
          onConfirm={remove}
          onCancel={() => setConfirming(false)}
        />
      )}
    </>
  );
}
