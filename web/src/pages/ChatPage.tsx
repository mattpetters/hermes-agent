/**
 * ChatPage — embeds `hermes --tui` inside the dashboard.
 *
 *   <div host> (dashboard chrome)                                         .
 *     └─ <div wrapper> (rounded, dark bg, padded — the "terminal window"  .
 *         look that gives the page a distinct visual identity)            .
 *         └─ @xterm/xterm Terminal (WebGL renderer, Unicode 11 widths)    .
 *              │ onData      keystrokes → WebSocket → PTY master          .
 *              │ onResize    terminal resize → `\x1b[RESIZE:cols;rows]`   .
 *              │ write(data) PTY output bytes → VT100 parser              .
 *              ▼                                                          .
 *     WebSocket /api/pty?token=<session>                                  .
 *          ▼                                                              .
 *     FastAPI pty_ws  (hermes_cli/web_server.py)                          .
 *          ▼                                                              .
 *     POSIX PTY → `node ui-tui/dist/entry.js` → tui_gateway + AIAgent     .
 *
 * Mobile UX notes (2026-04 changes):
 *
 *   - *Landing picker.* If the URL doesn't carry `?resume=…` we don't
 *     blindly spawn a new PTY any more; instead we render `ChatLanding`,
 *     which shows the active-session candidate (from localStorage) and
 *     lets the user resume it, start fresh, or browse history. This
 *     fixes iOS Safari "swipe-away then come back via the sidebar"
 *     accidentally killing the agent the user thought they were using.
 *
 *   - *URL self-heal.* The sidebar learns the live session id from the
 *     gateway and calls back into us; we `history.replaceState` the URL
 *     to `/chat?resume=<id>` so reload, back-nav, and bookmark all land
 *     on the right session.
 *
 *   - *Reconnect on visibility change.* Backgrounded WebSockets get
 *     killed by mobile Safari/Chrome; we detect the drop, wait for the
 *     page to become visible/online, and re-open with the live resume
 *     id so the user picks up where they left off (one re-spawned PTY
 *     with `--resume <id>`, not a brand-new conversation).
 */

import { FitAddon } from "@xterm/addon-fit";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { WebglAddon } from "@xterm/addon-webgl";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { Typography } from "@nous-research/ui";
import { cn } from "@/lib/utils";
import { Copy, PanelRight, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ChatLanding } from "@/components/ChatLanding";
import { ChatSidebar } from "@/components/ChatSidebar";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useI18n } from "@/i18n";
import { PluginSlot } from "@/plugins";
import {
  type ActiveChatSession,
  readActiveSession,
  writeActiveSession,
  clearActiveSession,
} from "@/lib/active-session";

function buildWsUrl(
  token: string,
  resume: string | null,
  channel: string,
): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const qs = new URLSearchParams({ token, channel });
  if (resume) qs.set("resume", resume);
  return `${proto}//${window.location.host}/api/pty?${qs.toString()}`;
}

// Channel id ties this chat tab's PTY child (publisher) to its sidebar
// (subscriber).  Generated once per mount so a tab refresh starts a fresh
// channel — the previous PTY child terminates with the old WS, and its
// channel auto-evicts when no subscribers remain.
function generateChannelId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `chat-${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`;
}

// Colors for the terminal body.  Matches the dashboard's dark teal canvas
// with cream foreground — we intentionally don't pick monokai or a loud
// theme, because the TUI's skin engine already paints the content; the
// terminal chrome just needs to sit quietly inside the dashboard.
const TERMINAL_THEME = {
  background: "#0d2626",
  foreground: "#f0e6d2",
  cursor: "#f0e6d2",
  cursorAccent: "#0d2626",
  selectionBackground: "#f0e6d244",
};

// Reconnect schedule for transient WS drops (iOS Safari backgrounding,
// Wi-Fi flap, server bounce). After MAX_ATTEMPTS we surface a banner and
// stop retrying — user can tap the banner to retry manually.
const RECONNECT_DELAYS_MS = [500, 1000, 2000, 4000, 8000];
const MAX_RECONNECT_ATTEMPTS = RECONNECT_DELAYS_MS.length;

/**
 * CSS width for xterm font tiers.
 *
 * Prefer the terminal host's `clientWidth` — Chrome DevTools device mode often
 * keeps `window.innerWidth` at the full desktop value while the *drawn* layout
 * is phone-sized, which made us pick desktop font sizes (~14px) and look huge.
 */
function terminalTierWidthPx(host: HTMLElement | null): number {
  if (typeof window === "undefined") return 1280;
  const fromHost = host?.clientWidth ?? 0;
  if (fromHost > 2) return Math.round(fromHost);
  const doc = document.documentElement?.clientWidth ?? 0;
  const vv = window.visualViewport;
  const inner = window.innerWidth;
  const vvw = vv?.width ?? inner;
  const layout = Math.min(inner, vvw, doc > 0 ? doc : inner);
  return Math.max(1, Math.round(layout));
}

function terminalFontSizeForWidth(layoutWidthPx: number): number {
  if (layoutWidthPx < 300) return 7;
  if (layoutWidthPx < 360) return 8;
  if (layoutWidthPx < 420) return 9;
  if (layoutWidthPx < 520) return 10;
  if (layoutWidthPx < 720) return 11;
  if (layoutWidthPx < 1024) return 12;
  return 14;
}

function terminalLineHeightForWidth(layoutWidthPx: number): number {
  return layoutWidthPx < 1024 ? 1.02 : 1.15;
}

type LaunchMode =
  // Either ?resume= was passed or the user explicitly chose resume/new.
  | "running"
  // Show the picker. Default for a bare `/chat` visit with a candidate
  // in localStorage.
  | "landing";
export default function ChatPage({ isActive = true }: { isActive?: boolean }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  // Exposed to the main metrics-sync effect so it can refit the terminal
  // the moment `isActive` flips back to true (display:none → display:flex
  // collapses the host's box, so ResizeObserver never fires on return).
  const syncMetricsRef = useRef<(() => void) | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  // Lazy-init: the missing-token check happens at construction so the effect
  // body doesn't have to setState (React 19's set-state-in-effect rule).
  const [banner, setBanner] = useState<string | null>(() =>
    typeof window !== "undefined" && !window.__HERMES_SESSION_TOKEN__
      ? "Session token unavailable. Open this page through `hermes dashboard`, not directly."
      : null,
  );
  const [copyState, setCopyState] = useState<"idle" | "copied">("idle");
  const copyResetRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Raw state for the mobile side-sheet + a derived value that force-
  // closes whenever the chat tab isn't active.  The *derived* value is
  // what side-effects (body-scroll lock, keydown listener, portal render)
  // key on — that way switching to another tab triggers the effect's
  // cleanup, releasing the scroll-lock on /sessions etc.  Returning to
  // /chat re-runs the effect (derived flips back to true) and re-locks.
  // Keying on the raw state would leak the body.overflow="hidden" across
  // tabs because the dep wouldn't change on tab switch.
  const [mobilePanelOpenRaw, setMobilePanelOpenRaw] = useState(false);
  const mobilePanelOpen = isActive && mobilePanelOpenRaw;
  const { setEnd } = usePageHeader();
  const { t } = useI18n();
  const closeMobilePanel = useCallback(() => setMobilePanelOpenRaw(false), []);
  const modelToolsLabel = useMemo(
    () => `${t.app.modelToolsSheetTitle} ${t.app.modelToolsSheetSubtitle}`,
    [t.app.modelToolsSheetSubtitle, t.app.modelToolsSheetTitle],
  );
  const [portalRoot] = useState<HTMLElement | null>(() =>
    typeof document !== "undefined" ? document.body : null,
  );
  const [narrow, setNarrow] = useState(() =>
    typeof window !== "undefined"
      ? window.matchMedia("(max-width: 1023px)").matches
      : false,
  );

  // The active resume id we'll pass to the next WebSocket open. Starts as
  // the URL `?resume=…` value; gets updated whenever the sidebar tells us
  // the live session id (via onSession), so a reconnect after backgrounding
  // re-attaches to the *current* session, not the one we initially launched
  // with.
  const resumeRef = useRef<string | null>(searchParams.get("resume"));
  const channel = useMemo(() => generateChannelId(), []);

  // Decide initial UX: if the URL has `?resume=` (or the user just clicked
  // a session-row's resume button) we launch straight into the TUI. Otherwise
  // we show ChatLanding — even for fresh users it's a single tap to start a
  // session, and for returning users they get the resume affordance instead
  // of an accidental fresh PTY.
  const initialResume = searchParams.get("resume");
  const [mode, setMode] = useState<LaunchMode>(() =>
    initialResume ? "running" : "landing",
  );
  const [candidate, setCandidate] = useState<ActiveChatSession | null>(() =>
    typeof window === "undefined" ? null : readActiveSession(),
  );

  // Reconnect bookkeeping: bump `wsEpoch` to force a new WebSocket while
  // keeping xterm alive. `attemptsRef` tracks consecutive failures so the
  // backoff progresses and we eventually stop.
  const [wsEpoch, setWsEpoch] = useState(0);
  const attemptsRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const scheduleReconnect = useCallback(() => {
    if (reconnectTimerRef.current) return;
    const idx = attemptsRef.current;
    if (idx >= MAX_RECONNECT_ATTEMPTS) {
      setBanner(
        "Disconnected — tap to retry, or refresh the page.",
      );
      return;
    }
    const delay = RECONNECT_DELAYS_MS[idx];
    attemptsRef.current = idx + 1;
    reconnectTimerRef.current = setTimeout(() => {
      reconnectTimerRef.current = null;
      setWsEpoch((n) => n + 1);
    }, delay);
  }, []);

  const cancelReconnect = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const handleManualRetry = useCallback(() => {
    attemptsRef.current = 0;
    setBanner(null);
    cancelReconnect();
    setWsEpoch((n) => n + 1);
  }, [cancelReconnect]);

  // Sidebar callback — record the live session id, persist it for next
  // visit, and rewrite the URL so reload/back/refresh land here.
  const handleSession = useCallback(
    (sessionId: string) => {
      resumeRef.current = sessionId;
      writeActiveSession(sessionId);
      // Avoid React Router round-trip; this is a pure URL nicety so a
      // reload picks up the right session.
      try {
        const url = new URL(window.location.href);
        if (url.searchParams.get("resume") !== sessionId) {
          url.searchParams.set("resume", sessionId);
          window.history.replaceState(window.history.state, "", url.toString());
          // Keep React Router state in sync (it doesn't observe replaceState).
          setSearchParams({ resume: sessionId }, { replace: true });
        }
      } catch {
        /* ignore */
      }
    },
    [setSearchParams],
  );

  useEffect(() => {
    const mql = window.matchMedia("(max-width: 1023px)");
    const sync = () => setNarrow(mql.matches);
    sync();
    mql.addEventListener("change", sync);
    return () => mql.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (!mobilePanelOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeMobilePanel();
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [mobilePanelOpen, closeMobilePanel]);

  useEffect(() => {
    const mql = window.matchMedia("(min-width: 1024px)");
    const onChange = (e: MediaQueryListEvent) => {
      if (e.matches) setMobilePanelOpenRaw(false);
    };
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    // When hidden (non-chat tab) we must not register the header button —
    // another page owns the header's end slot at that point.
    if (!isActive) {
      setEnd(null);
      return;
    }
    if (!narrow || mode !== "running") {
      setEnd(null);
      return;
    }
    setEnd(
      <button
        type="button"
        onClick={() => setMobilePanelOpenRaw(true)}
        className={cn(
          "inline-flex items-center gap-1.5 rounded border border-current/20",
          "px-2.5 py-1.5 text-[0.7rem] font-medium tracking-wide normal-case",
          // Touch-comfortable target — Apple HIG recommends 44px, we
          // give the icon a 32px hit box plus the label as visual tail
          // so the whole thing reads ~44px wide.
          "min-h-[32px] text-midground/80 hover:text-midground hover:bg-midground/5",
          "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground",
          "shrink-0 cursor-pointer",
        )}
        aria-expanded={mobilePanelOpen}
        aria-controls="chat-side-panel"
      >
        <PanelRight className="h-3.5 w-3.5 shrink-0" />
        {modelToolsLabel}
      </button>,
    );
    return () => setEnd(null);
  }, [isActive, narrow, mode, mobilePanelOpen, modelToolsLabel, setEnd]);

  const handleCopyLast = () => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    // Send the slash as a burst, wait long enough for Ink's tokenizer to
    // emit a keypress event for each character (not coalesce them into a
    // paste), then send Return as its own event.  The timing here is
    // empirical — 100ms is safely past Node's default stdin coalescing
    // window and well inside UI responsiveness.
    ws.send("/copy");
    setTimeout(() => {
      const s = wsRef.current;
      if (s && s.readyState === WebSocket.OPEN) s.send("\r");
    }, 100);
    setCopyState("copied");
    if (copyResetRef.current) clearTimeout(copyResetRef.current);
    copyResetRef.current = setTimeout(() => setCopyState("idle"), 1500);
    termRef.current?.focus();
  };

  // ---- Landing-picker handlers --------------------------------------
  const handleStartNew = useCallback(() => {
    resumeRef.current = null;
    clearActiveSession();
    setCandidate(null);
    attemptsRef.current = 0;
    setBanner(null);
    // Scrub `?resume=` from the URL — we want a fresh launch.
    try {
      const url = new URL(window.location.href);
      if (url.searchParams.has("resume")) {
        url.searchParams.delete("resume");
        window.history.replaceState(window.history.state, "", url.toString());
      }
    } catch {
      /* ignore */
    }
    setMode("running");
  }, []);

  const handleResumeCandidate = useCallback(
    (id: string) => {
      resumeRef.current = id;
      attemptsRef.current = 0;
      setBanner(null);
      // Reflect resume in the URL so refresh keeps working.
      navigate(`/chat?resume=${encodeURIComponent(id)}`, { replace: true });
      setMode("running");
    },
    [navigate],
  );

  // ---- Terminal lifecycle (mount once per "running" launch) ---------
  //
  // We split terminal creation from WebSocket lifecycle so a transient
  // WS drop doesn't blow away the user's scrollback. Terminal owns
  // sizing, clipboard, motion-dedup, key bindings; the WS effect just
  // pumps bytes both directions.
  useEffect(() => {
    if (mode !== "running") return;
    const host = hostRef.current;
    if (!host) return;

    const token = window.__HERMES_SESSION_TOKEN__;
    if (!token) return;

    const tierW0 = terminalTierWidthPx(host);
    const term = new Terminal({
      allowProposedApi: true,
      cursorBlink: true,
      fontFamily:
        "'JetBrains Mono', 'Cascadia Mono', 'Fira Code', 'MesloLGS NF', 'Source Code Pro', Menlo, Consolas, 'DejaVu Sans Mono', monospace",
      fontSize: terminalFontSizeForWidth(tierW0),
      lineHeight: terminalLineHeightForWidth(tierW0),
      letterSpacing: 0,
      fontWeight: "400",
      fontWeightBold: "700",
      macOptionIsMeta: true,
      scrollback: 0,
      theme: TERMINAL_THEME,
    });
    termRef.current = term;

    // --- Clipboard integration ---------------------------------------
    //
    // Three independent paths all route to the system clipboard:
    //
    //   1. **Selection → Ctrl+C (or Cmd+C on macOS).**  Ink's own handler
    //      in useInputHandlers.ts turns Ctrl+C into a copy when the
    //      terminal has a selection, then emits an OSC 52 escape.  Our
    //      OSC 52 handler below decodes that escape and writes to the
    //      browser clipboard — so the flow works just like it does in
    //      `hermes --tui`.
    //
    //   2. **Ctrl/Cmd+Shift+C.**  Belt-and-suspenders shortcut that
    //      operates directly on xterm's selection, useful if the TUI
    //      ever stops listening (e.g. overlays / pickers) or if the user
    //      has selected with the mouse outside of Ink's selection model.
    //
    //   3. **Ctrl/Cmd+Shift+V.**  Reads the system clipboard and feeds
    //      it to the terminal as keyboard input.  xterm's paste() wraps
    //      it with bracketed-paste if the host has that mode enabled.
    //
    // OSC 52 reads (terminal asking to read the clipboard) are not
    // supported — that would let any content the TUI renders exfiltrate
    // the user's clipboard.
    term.parser.registerOscHandler(52, (data) => {
      const semi = data.indexOf(";");
      if (semi < 0) return false;
      const payload = data.slice(semi + 1);
      if (payload === "?" || payload === "") return false;
      try {
        const binary = atob(payload);
        const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
        const text = new TextDecoder("utf-8").decode(bytes);
        navigator.clipboard.writeText(text).catch((err) => {
          console.warn("[dashboard clipboard] OSC 52 write failed:", err.message);
        });
      } catch {
        console.warn("[dashboard clipboard] malformed OSC 52 payload");
      }
      return true;
    });

    const isMac =
      typeof navigator !== "undefined" && /Mac/i.test(navigator.platform);

    term.attachCustomKeyEventHandler((ev) => {
      if (ev.type !== "keydown") return true;

      const copyModifier = isMac ? ev.metaKey : ev.ctrlKey && ev.shiftKey;
      const pasteModifier = isMac ? ev.metaKey : ev.ctrlKey && ev.shiftKey;

      if (copyModifier && ev.key.toLowerCase() === "c") {
        const sel = term.getSelection();
        if (sel) {
          navigator.clipboard.writeText(sel).catch((err) => {
            console.warn("[dashboard clipboard] direct copy failed:", err.message);
          });
          term.clearSelection();
          ev.preventDefault();
          return false;
        }
      }

      if (pasteModifier && ev.key.toLowerCase() === "v") {
        navigator.clipboard
          .readText()
          .then((text) => {
            if (text) term.paste(text);
          })
          .catch((err) => {
            console.warn("[dashboard clipboard] paste failed:", err.message);
          });
        ev.preventDefault();
        return false;
      }

      return true;
    });

    const fit = new FitAddon();
    fitRef.current = fit;
    term.loadAddon(fit);

    const unicode11 = new Unicode11Addon();
    term.loadAddon(unicode11);
    term.unicode.activeVersion = "11";

    term.loadAddon(new WebLinksAddon());

    term.open(host);

    const useWebgl = terminalTierWidthPx(host) >= 768;
    if (useWebgl) {
      try {
        const webgl = new WebglAddon();
        webgl.onContextLoss(() => webgl.dispose());
        term.loadAddon(webgl);
      } catch (err) {
        console.warn(
          "[hermes-chat] WebGL renderer unavailable; falling back to default",
          err,
        );
      }
    }

    // Initial fit + resize observer — see commit history for the rAF dance.
    let hostSyncRaf = 0;
    const scheduleHostSync = () => {
      if (hostSyncRaf) return;
      hostSyncRaf = requestAnimationFrame(() => {
        hostSyncRaf = 0;
        syncTerminalMetrics();
      });
    };

    let metricsDebounce: ReturnType<typeof setTimeout> | null = null;
    const syncTerminalMetrics = () => {
      // display:none hosts have clientWidth/Height = 0, which fit() turns
      // into a 1x1 terminal.  Skip entirely while hidden; the visibility
      // effect below runs another fit as soon as the tab is shown again.
      if (!host.isConnected || host.clientWidth <= 0 || host.clientHeight <= 0) {
        return;
      }
      const w = terminalTierWidthPx(host);
      const nextSize = terminalFontSizeForWidth(w);
      const nextLh = terminalLineHeightForWidth(w);
      const fontChanged =
        term.options.fontSize !== nextSize ||
        term.options.lineHeight !== nextLh;
      if (fontChanged) {
        term.options.fontSize = nextSize;
        term.options.lineHeight = nextLh;
      }
      try {
        fit.fit();
      } catch {
        return;
      }
      if (fontChanged && term.rows > 0) {
        try {
          term.refresh(0, term.rows - 1);
        } catch {
          /* ignore */
        }
      }
      if (
        fontChanged &&
        wsRef.current &&
        wsRef.current.readyState === WebSocket.OPEN
      ) {
        wsRef.current.send(`\x1b[RESIZE:${term.cols};${term.rows}]`);
      }
    };
    syncMetricsRef.current = syncTerminalMetrics;

    const scheduleSyncTerminalMetrics = () => {
      if (metricsDebounce) clearTimeout(metricsDebounce);
      metricsDebounce = setTimeout(() => {
        metricsDebounce = null;
        syncTerminalMetrics();
      }, 60);
    };

    const ro = new ResizeObserver(() => scheduleHostSync());
    ro.observe(host);

    window.addEventListener("resize", scheduleSyncTerminalMetrics);
    window.visualViewport?.addEventListener("resize", scheduleSyncTerminalMetrics);
    window.visualViewport?.addEventListener("scroll", scheduleSyncTerminalMetrics);
    scheduleHostSync();
    requestAnimationFrame(() => scheduleHostSync());

    let settleRaf1 = 0;
    let settleRaf2 = 0;
    settleRaf1 = requestAnimationFrame(() => {
      settleRaf1 = 0;
      settleRaf2 = requestAnimationFrame(() => {
        settleRaf2 = 0;
        syncTerminalMetrics();
      });
    });

    // Forward keystrokes + mouse events. We read wsRef each call because
    // the WS gets replaced on reconnect (see WebSocket effect below).
    // eslint-disable-next-line no-control-regex -- intentional ESC byte in xterm SGR mouse report parser
    const SGR_MOUSE_RE = /^\x1b\[<(\d+);(\d+);(\d+)([Mm])$/;
    let lastMotionCell = { col: -1, row: -1 };
    let lastMotionCb = -1;
    const onDataDisposable = term.onData((data) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;

      const m = SGR_MOUSE_RE.exec(data);
      if (m) {
        const cb = parseInt(m[1], 10);
        const col = parseInt(m[2], 10);
        const row = parseInt(m[3], 10);
        const released = m[4] === "m";
        const isMotion = (cb & 0x20) !== 0 && (cb & 0x40) === 0;
        const isWheel = (cb & 0x40) !== 0;
        if (isMotion && !isWheel && !released) {
          if (
            col === lastMotionCell.col &&
            row === lastMotionCell.row &&
            cb === lastMotionCb
          ) {
            return;
          }
          lastMotionCell = { col, row };
          lastMotionCb = cb;
        } else {
          lastMotionCell = { col: -1, row: -1 };
          lastMotionCb = -1;
        }
      }

      ws.send(data);
    });

    const onResizeDisposable = term.onResize(({ cols, rows }) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(`\x1b[RESIZE:${cols};${rows}]`);
      }
    });

    term.focus();

    return () => {
      syncMetricsRef.current = null;
      onDataDisposable.dispose();
      onResizeDisposable.dispose();
      if (metricsDebounce) clearTimeout(metricsDebounce);
      window.removeEventListener("resize", scheduleSyncTerminalMetrics);
      window.visualViewport?.removeEventListener(
        "resize",
        scheduleSyncTerminalMetrics,
      );
      window.visualViewport?.removeEventListener(
        "scroll",
        scheduleSyncTerminalMetrics,
      );
      ro.disconnect();
      if (hostSyncRaf) cancelAnimationFrame(hostSyncRaf);
      if (settleRaf1) cancelAnimationFrame(settleRaf1);
      if (settleRaf2) cancelAnimationFrame(settleRaf2);
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
      if (copyResetRef.current) {
        clearTimeout(copyResetRef.current);
        copyResetRef.current = null;
      }
    };
  }, [mode]);

  // ---- WebSocket lifecycle (re-runs on reconnect) -------------------
  useEffect(() => {
    if (mode !== "running") return;
    const term = termRef.current;
    if (!term) return;

    const token = window.__HERMES_SESSION_TOKEN__;
    if (!token) return;

    const url = buildWsUrl(token, resumeRef.current, channel);
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    let unmounting = false;

    ws.onopen = () => {
      attemptsRef.current = 0;
      setBanner(null);
      // Send the initial RESIZE immediately so Ink has *a* size to lay
      // out against on its first paint. The double-rAF block in the
      // terminal effect will follow up with the authoritative one.
      try {
        ws.send(`\x1b[RESIZE:${term.cols};${term.rows}]`);
      } catch {
        /* ignore */
      }
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        term.write(ev.data);
      } else {
        term.write(new Uint8Array(ev.data as ArrayBuffer));
      }
    };

    ws.onclose = (ev) => {
      wsRef.current = null;
      if (unmounting) return;

      // Auth / loopback / server-fatal: don't bother reconnecting.
      if (ev.code === 4401) {
        setBanner("Auth failed. Reload the page to refresh the session token.");
        return;
      }
      if (ev.code === 4403) {
        setBanner(
          "Chat is restricted to localhost on this dashboard. Pass --insecure to expose it on the LAN.",
        );
        return;
      }
      if (ev.code === 1011) {
        // Server already wrote an ANSI error frame.
        return;
      }

      // Transient close — try to reconnect with backoff. With a known
      // resume id we re-attach to the same TUI session via `--resume`.
      // On the first reconnect attempt we don't paint `[session ended]`
      // because in the typical case (mobile background → foreground) the
      // user will see the same content reappear once the new WS hands
      // the PTY's repaint.
      if (attemptsRef.current === 0) {
        term.write(
          "\r\n\x1b[90m[disconnected — reconnecting…]\x1b[0m\r\n",
        );
      }
      scheduleReconnect();
    };

    return () => {
      unmounting = true;
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      if (wsRef.current === ws) {
        wsRef.current = null;
      }
    };
  }, [mode, channel, wsEpoch, scheduleReconnect]);

  // ---- Mobile-friendly reconnect triggers ---------------------------
  // iOS Safari kills WebSockets when the page goes to the background;
  // when the user comes back we want to attempt reconnect immediately
  // (don't sit on the backoff timer waiting). Also recover cleanly when
  // the device flips from offline to online.
  useEffect(() => {
    if (mode !== "running") return;
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return;
      attemptsRef.current = 0;
      cancelReconnect();
      setWsEpoch((n) => n + 1);
    };
    const onOnline = () => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return;
      attemptsRef.current = 0;
      cancelReconnect();
      setWsEpoch((n) => n + 1);
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("pageshow", onVisible);
    window.addEventListener("online", onOnline);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("pageshow", onVisible);
      window.removeEventListener("online", onOnline);
    };
  }, [mode, cancelReconnect]);

  // Cancel pending reconnect timer on unmount.
  useEffect(() => {
    return () => cancelReconnect();
  }, [cancelReconnect]);

  // When the user returns to the chat tab (isActive: false → true), the
  // terminal host just transitioned from display:none to display:flex.
  // ResizeObserver won't fire on that kind of style-driven box change —
  // xterm thinks its grid is still whatever it was when the tab was
  // hidden (or 0×0, if it was hidden before first fit).  Force a refit
  // after two animation frames so layout has committed.
  //
  // Focus handling: we only steal focus back into the terminal when
  // nothing else inside ChatPage was holding it (typically the first
  // activation after mount, where document.activeElement is <body>; or
  // a return after the user had been typing in the terminal, where
  // focus was already on the xterm textarea before the tab got hidden
  // and has since fallen back to <body>).  If the user had clicked
  // into the sidebar (model picker, tool-call entry) before switching
  // tabs, we must not yank focus away from wherever they left it when
  // they come back — that's a surprise and an a11y foot-gun.
  useEffect(() => {
    if (!isActive || mode !== "running") return;
    let raf1 = 0;
    let raf2 = 0;
    raf1 = requestAnimationFrame(() => {
      raf1 = 0;
      raf2 = requestAnimationFrame(() => {
        raf2 = 0;
        syncMetricsRef.current?.();
        const host = hostRef.current;
        const active = typeof document !== "undefined"
          ? document.activeElement
          : null;
        const focusIsElsewhereInChatPage =
          active !== null &&
          active !== document.body &&
          host !== null &&
          !host.contains(active);
        if (!focusIsElsewhereInChatPage) {
          termRef.current?.focus();
        }
      });
    });
    return () => {
      if (raf1) cancelAnimationFrame(raf1);
      if (raf2) cancelAnimationFrame(raf2);
    };
  }, [isActive, mode]);

  // Layout:
  //   outer flex column — sits inside the dashboard's content area
  //   row split — terminal pane (flex-1) + sidebar (fixed width, lg+)
  //   terminal wrapper — rounded, dark, padded — the "terminal window"
  //   floating copy button — bottom-right corner, transparent with a
  //     subtle border; stays out of the way until hovered.  Sends
  //     `/copy\n` to Ink, which emits OSC 52 → our clipboard handler.
  //   sidebar — ChatSidebar opens its own JSON-RPC sidecar; renders
  //     model badge, tool-call list, model picker. Best-effort: if the
  //     sidecar fails to connect the terminal pane keeps working.
  //
  // `normal-case` opts out of the dashboard's global `uppercase` rule on
  // the root `<div>` in App.tsx — terminal output must preserve case.
  //
  // Mobile model/tools sheet is portaled to `document.body` so it stacks
  // above the app sidebar (`z-50`) and mobile chrome (`z-40`).
  const mobileModelToolsPortal =
    isActive &&
    narrow &&
    portalRoot &&
    mode === "running" &&
    createPortal(
      <>
        {mobilePanelOpen && (
          <button
            type="button"
            aria-label={t.app.closeModelTools}
            onClick={closeMobilePanel}
            className={cn(
              "fixed inset-0 z-[55]",
              "bg-black/60 backdrop-blur-sm cursor-pointer",
            )}
          />
        )}

        <div
          id="chat-side-panel"
          role="complementary"
          aria-label={modelToolsLabel}
          className={cn(
            "font-mondwest fixed top-0 right-0 z-[60] flex h-dvh max-h-dvh w-72 max-w-[88vw] min-w-0 flex-col antialiased",
            "border-l border-current/20 text-midground",
            "bg-background-base/95 backdrop-blur-sm",
            "transition-transform duration-200 ease-out",
            "[background:var(--component-sidebar-background)]",
            "[clip-path:var(--component-sidebar-clip-path)]",
            "[border-image:var(--component-sidebar-border-image)]",
            mobilePanelOpen
              ? "translate-x-0"
              : "pointer-events-none translate-x-full",
          )}
        >
          <div
            className={cn(
              "flex h-14 shrink-0 items-center justify-between gap-2 border-b border-current/20 px-5",
            )}
          >
            <Typography
              className="font-bold text-[1.125rem] leading-[0.95] tracking-[0.0525rem] text-midground"
              style={{ mixBlendMode: "plus-lighter" }}
            >
              {t.app.modelToolsSheetTitle}
              <br />
              {t.app.modelToolsSheetSubtitle}
            </Typography>

            <button
              type="button"
              onClick={closeMobilePanel}
              aria-label={t.app.closeModelTools}
              className={cn(
                // 40px hit box — comfortable on touch.
                "inline-flex h-10 w-10 items-center justify-center -mr-2",
                "text-midground/70 hover:text-midground transition-colors cursor-pointer",
                "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-midground",
              )}
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <div
            className={cn(
              "min-h-0 flex-1 overflow-y-auto overflow-x-hidden",
              "border-t border-current/10",
            )}
          >
            <ChatSidebar channel={channel} onSession={handleSession} />
          </div>
        </div>
      </>,
      portalRoot,
    );

  // ---- Render -------------------------------------------------------
  if (mode === "landing") {
    return (
      <div className="flex min-h-0 flex-1 flex-col gap-2 normal-case">
        <PluginSlot name="chat:top" />
        <ChatLanding
          candidate={candidate}
          onResume={handleResumeCandidate}
          onStartNew={handleStartNew}
        />
        <PluginSlot name="chat:bottom" />
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2 normal-case">
      <PluginSlot name="chat:top" />
      {mobileModelToolsPortal}

      {banner && (
        <button
          type="button"
          onClick={handleManualRetry}
          className={cn(
            "border border-warning/50 bg-warning/10 text-warning",
            "px-3 py-2 text-xs tracking-wide cursor-pointer text-left",
            "hover:bg-warning/20 transition-colors",
          )}
          title="Tap to retry connection"
        >
          {banner}
        </button>
      )}

      <div className="flex min-h-0 flex-1 flex-col gap-2 lg:flex-row lg:gap-3">
        <div
          className={cn(
            "relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-lg",
            "p-2 sm:p-3",
          )}
          style={{
            backgroundColor: TERMINAL_THEME.background,
            boxShadow: "0 8px 32px rgba(0, 0, 0, 0.4)",
          }}
        >
          <div
            ref={hostRef}
            className="hermes-chat-xterm-host min-h-0 min-w-0 flex-1"
          />

          <button
            type="button"
            onClick={handleCopyLast}
            title="Copy last assistant response as raw markdown"
            aria-label="Copy last assistant response"
            className={cn(
              "absolute z-10 flex items-center gap-1.5",
              "rounded border border-current/30",
              "bg-black/20 backdrop-blur-sm",
              "opacity-60 hover:opacity-100 hover:border-current/60",
              "transition-opacity duration-150",
              "focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-current",
              "cursor-pointer",
              "bottom-2 right-2 px-2 py-1.5 text-[0.7rem] sm:bottom-3 sm:right-3 sm:px-2.5 sm:py-1.5 sm:text-xs",
              "lg:bottom-4 lg:right-4",
            )}
            style={{ color: TERMINAL_THEME.foreground }}
          >
            <Copy className="h-3 w-3 shrink-0" />
            <span className="hidden min-[400px]:inline tracking-wide">
              {copyState === "copied" ? "copied" : "copy last response"}
            </span>
          </button>
        </div>

        {!narrow && (
          <div
            id="chat-side-panel"
            role="complementary"
            aria-label={modelToolsLabel}
            className="flex min-h-0 shrink-0 flex-col lg:h-full lg:w-80"
          >
            <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
              <ChatSidebar channel={channel} onSession={handleSession} />
            </div>
          </div>
        )}
      </div>
      <PluginSlot name="chat:bottom" />
    </div>
  );
}

declare global {
  interface Window {
    __HERMES_SESSION_TOKEN__?: string;
  }
}
