import type { Viseme } from "../lib/visemes";

export type AvatarState = "idle" | "listening" | "thinking" | "speaking" | "muted";

/** What a renderer (2D SVG or 3D GLB) needs; the wrapper owns the panel chrome. */
export interface RendererProps {
  state: AvatarState;
  getViseme: () => { v: Viseme; w: number };
  label: string;
  stateLabel: string;
}
