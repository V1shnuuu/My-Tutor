/**
 * LiveAvatar (HeyGen real-time streaming) client — local/dev-only. See
 * core/app/liveavatar.py for why this can't be the free, no-quota default at scale.
 *
 * The browser never sees the LiveAvatar API key: core mints a session (POST
 * /avatar/live/session) and hands back only a LiveKit room + short-lived client token.
 * From there this class drives the avatar itself over the LiveKit data channel — the
 * exact envelope below was reverse-engineered by connecting to a real (sandboxed) session
 * and observing traffic, since LiveAvatar's public docs omit it:
 *
 *   agent-control (publish): {event_id, event_type: "avatar.speak_text"|"avatar.interrupt", session_id, text?}
 *   agent-response (subscribe): {event_type: "avatar.speak_started"|"avatar.speak_ended"|"session.stopped"|..., session_id, source_event_id, ...}
 */
import { Room, RoomEvent, Track } from "livekit-client";
import { API } from "./api";
import type { Lang } from "./api";

export type LiveAvatarState = "connecting" | "idle" | "speaking" | "error" | "closed";

interface SessionResp {
  session_id: string;
  livekit_url: string;
  livekit_client_token: string;
  max_session_duration: number;
  sandbox: boolean;
}

export class LiveAvatarSession {
  private room = new Room();
  private sessionId: string | null = null;
  private token: string;
  private videoEl: HTMLVideoElement | null = null;
  state: LiveAvatarState = "connecting";
  onState: (s: LiveAvatarState) => void = () => {};

  constructor(token: string) {
    this.token = token;
  }

  private setState(s: LiveAvatarState) {
    if (s !== this.state) { this.state = s; this.onState(s); }
  }

  /** Connects, waits for the avatar participant, and attaches its tracks to `video`.
   *  `muted` starts the audio track silenced — used while pre-warming a replacement
   *  session in the background so it doesn't talk over the one currently on screen;
   *  call `unmute()` once it takes over. */
  async start(lang: Lang, video: HTMLVideoElement, muted = false): Promise<{ sandbox: boolean; maxSessionDuration: number }> {
    const r = await fetch(`${API}/avatar/live/session`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${this.token}` },
      body: JSON.stringify({ lang }),
    });
    if (!r.ok) throw new Error(`live avatar session: HTTP ${r.status}`);
    const s: SessionResp = await r.json();
    this.sessionId = s.session_id;
    this.videoEl = video;
    video.muted = muted;

    // Video and audio arrive as two separate LiveKit tracks. Attaching audio with no element
    // makes LiveKit create its own independent <audio> element — a second playback clock
    // that drifts from the video's over time and is exactly what reads as bad lip sync.
    // Attaching both tracks to the *same* <video> element combines them onto one MediaStream
    // so the browser keeps them on a single clock, the way a real A/V stream would be.
    this.room.on(RoomEvent.TrackSubscribed, (track) => {
      if (track.kind === Track.Kind.Video || track.kind === Track.Kind.Audio) track.attach(video);
    });
    this.room.on(RoomEvent.DataReceived, (payload, _p, _k, topic) => {
      if (topic !== "agent-response") return;
      try {
        const ev = JSON.parse(new TextDecoder().decode(payload));
        if (ev.event_type === "avatar.speak_started") this.setState("speaking");
        else if (ev.event_type === "avatar.speak_ended") this.setState("idle");
        else if (ev.event_type === "session.stopped" || ev.event_type === "session.stop") this.setState("closed");
      } catch { /* ignore malformed frames */ }
    });
    this.room.on(RoomEvent.Disconnected, () => this.setState("closed"));

    await this.room.connect(s.livekit_url, s.livekit_client_token);
    // The agent joins the room asynchronously right after connect; publishing before it
    // arrives silently drops the message (LiveKit data channels don't queue for late
    // joiners), so wait for it rather than racing.
    const deadline = Date.now() + 8000;
    while (this.room.remoteParticipants.size === 0 && Date.now() < deadline) {
      await new Promise((res) => setTimeout(res, 100));
    }
    if (this.room.remoteParticipants.size === 0) throw new Error("live avatar: agent never joined");
    this.setState("idle");
    return { sandbox: s.sandbox, maxSessionDuration: s.max_session_duration };
  }

  /** Un-silences a session that was pre-warmed muted, once it becomes the visible one. */
  unmute() { if (this.videoEl) this.videoEl.muted = false; }

  private publish(eventType: string, extra: Record<string, unknown> = {}) {
    if (!this.sessionId) return;
    const payload = { event_id: crypto.randomUUID(), event_type: eventType, session_id: this.sessionId, ...extra };
    void this.room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify(payload)), {
      reliable: true,
      topic: "agent-control",
    });
  }

  /** Queues a sentence for LiveAvatar's own TTS + lip-synced video (no Piper involved). */
  speakText(text: string) {
    this.publish("avatar.speak_text", { text });
  }

  /** Cancels whatever is queued/playing — mirrors Speaker.stop(). */
  interrupt() {
    this.publish("avatar.interrupt");
  }

  async stop() {
    const id = this.sessionId;
    this.sessionId = null;
    this.room.disconnect();
    this.setState("closed");
    if (id) {
      try {
        await fetch(`${API}/avatar/live/session/${id}/stop`, {
          method: "POST",
          headers: { authorization: `Bearer ${this.token}` },
        });
      } catch { /* best-effort */ }
    }
  }
}
