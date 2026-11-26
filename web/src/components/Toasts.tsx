import { useEffect } from "react";
import { useAppState, useDispatch } from "../state/store";
import type { Toast } from "../state/reducer";

export function Toasts() {
  const toasts = useAppState((s) => s.toasts);
  if (toasts.length === 0) return null;
  return (
    <div className="toasts" aria-live="assertive">
      {toasts.map((t) => (
        <ToastItem key={t.id} toast={t} />
      ))}
    </div>
  );
}

function ToastItem({ toast }: { toast: Toast }) {
  const dispatch = useDispatch();
  useEffect(() => {
    const t = setTimeout(() => dispatch({ type: "toast_dismiss", id: toast.id }), toast.level === "error" ? 8000 : 4000);
    return () => clearTimeout(t);
  }, [dispatch, toast.id, toast.level]);
  return (
    <div className={`toast toast-${toast.level}`} role={toast.level === "error" ? "alert" : "status"} data-testid="toast">
      <span className="toast-msg">{toast.message}</span>
      <button className="icon-btn" aria-label="Dismiss" onClick={() => dispatch({ type: "toast_dismiss", id: toast.id })}>
        ×
      </button>
    </div>
  );
}
