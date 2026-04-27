/**
 * ChatLanding — small mobile-first picker shown when /chat mounts
 * with no `?resume=` and we want to confirm whether to resume the
 * last known session or start a fresh one.
 *
 * On phones the implicit "spawn new TUI on every visit" behavior is
 * confusing: tapping the Chat tab from the sidebar after backgrounding
 * Safari kills the agent the user thought they were chatting with.
 * This picker makes the choice explicit *and* keeps it fast — one tap
 * to resume, one tap to start fresh, one tap to browse history.
 *
 * Desktop sees this too (it's the same UX), but it's only shown when
 * we genuinely don't know what session to land on.
 */

import { useNavigate } from "react-router-dom";
import { Play, Plus, History } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  type ActiveChatSession,
  freshnessLabel,
} from "@/lib/active-session";
import { cn } from "@/lib/utils";

interface ChatLandingProps {
  /** The persisted session, if we have one fresh enough to offer. */
  candidate: ActiveChatSession | null;
  /** Caller starts a fresh session by setting `resume=null`. */
  onStartNew: () => void;
  /** Caller resumes the candidate session. */
  onResume: (id: string) => void;
}

export function ChatLanding({ candidate, onStartNew, onResume }: ChatLandingProps) {
  const navigate = useNavigate();

  return (
    <div
      className={cn(
        "flex min-h-0 flex-1 flex-col items-stretch justify-center gap-4 p-4",
        "mx-auto w-full max-w-md",
      )}
    >
      <Card className="flex flex-col gap-1 px-4 py-3">
        <div className="text-xs uppercase tracking-wider text-muted-foreground">
          chat
        </div>
        <div className="text-sm">
          {candidate ? (
            <>
              You have an active session from{" "}
              <span className="font-medium">
                {freshnessLabel(candidate.updatedAt)}
              </span>
              . Resume it, or start fresh?
            </>
          ) : (
            "Start a new chat session, or browse past sessions to pick one up."
          )}
        </div>
      </Card>

      <div className="flex flex-col gap-2">
        {candidate && (
          <Button
            type="button"
            variant="default"
            // Big touch targets on mobile — the dashboard's default Button is fine
            // size-wise (h-9), but on phones we want comfortably larger taps.
            className="h-12 justify-start gap-3 px-4 text-base"
            onClick={() => onResume(candidate.id)}
          >
            <Play className="h-4 w-4 shrink-0" />
            <span className="flex flex-col items-start text-left">
              <span className="font-medium leading-tight">Resume session</span>
              <span className="text-xs opacity-80 leading-tight">
                {candidate.title ?? candidate.id.slice(0, 24)}
              </span>
            </span>
          </Button>
        )}

        <Button
          type="button"
          variant={candidate ? "outline" : "default"}
          className="h-12 justify-start gap-3 px-4 text-base"
          onClick={onStartNew}
        >
          <Plus className="h-4 w-4 shrink-0" />
          <span className="font-medium">Start new session</span>
        </Button>

        <Button
          type="button"
          variant="ghost"
          className="h-12 justify-start gap-3 px-4 text-base"
          onClick={() => navigate("/sessions")}
        >
          <History className="h-4 w-4 shrink-0" />
          <span className="font-medium">Browse sessions…</span>
        </Button>
      </div>
    </div>
  );
}
