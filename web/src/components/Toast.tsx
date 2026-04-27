import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

export function Toast({ toast }: { toast: { message: string; type: "success" | "error" } | null }) {
  const [visible, setVisible] = useState(false);
  const [current, setCurrent] = useState(toast);

  useEffect(() => {
    // Sync external toast prop into local state; needed so `current` can
    // persist through the 200ms fade-out animation after `toast` becomes null.
    /* eslint-disable react-hooks/set-state-in-effect */
    if (toast) {
      setCurrent(toast);
      setVisible(true);
    } else {
      setVisible(false);
      const timer = setTimeout(() => setCurrent(null), 200);
      return () => clearTimeout(timer);
    }
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [toast]);

  if (!current) return null;

  // Portal to document.body so the toast escapes any ancestor stacking context
  // (e.g. <main> has `relative z-2`, which would trap z-50 below the header's z-40).
  return createPortal(
    <div
      role="status"
      aria-live="polite"
      className={`fixed top-16 right-4 z-50 border px-4 py-2.5 font-courier text-xs tracking-wider uppercase backdrop-blur-sm ${
        current.type === "success"
          ? "bg-success/15 text-success border-success/30"
          : "bg-destructive/15 text-destructive border-destructive/30"
      }`}
      style={{
        animation: visible ? "toast-in 200ms ease-out forwards" : "toast-out 200ms ease-in forwards",
      }}
    >
      {current.message}
    </div>,
    document.body,
  );
}
