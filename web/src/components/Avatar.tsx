import { Suspense, lazy, useCallback, useState } from "react";
import Avatar2D from "./Avatar2D";
import type { AvatarState, RendererProps } from "./avatarTypes";

export type { AvatarState };

const Avatar3D = lazy(() => import("./Avatar3D"));

interface Props extends RendererProps {
  muteLabel?: string;
  onUnmute?: () => void;
}

/** WebGL2 is required for the GLB; without it the 2D face is the whole experience. */
function webglOK(): boolean {
  try {
    return !!document.createElement("canvas").getContext("webgl2");
  } catch {
    return false;
  }
}

/**
 * Picks the renderer: the 3D humanoid when WebGL and the GLB are both available, otherwise
 * the 2D face. The 2D face also covers the gap while the GLB downloads, so the panel is
 * never empty, and both are driven by the same viseme clock so the swap is seamless.
 */
export default function Avatar({ state, getViseme, label, stateLabel, muteLabel, onUnmute }: Props) {
  const [use3d, setUse3d] = useState(webglOK);
  const [ready, setReady] = useState(false);
  const onFail = useCallback(() => { setUse3d(false); setReady(false); }, []);
  const onReady = useCallback(() => setReady(true), []);
  const renderer = { state, getViseme, label, stateLabel };

  return (
    <div className="avatar-wrap">
      {use3d && (
        <Suspense fallback={null}>
          <Avatar3D {...renderer} onReady={onReady} onFail={onFail} />
        </Suspense>
      )}
      {!ready && <Avatar2D {...renderer} />}
      <span className={`avatar-state ${state}`} aria-hidden="true">{stateLabel}</span>
      {state === "muted" && muteLabel && (
        <button className="mute-badge" onClick={onUnmute}>{muteLabel}</button>
      )}
    </div>
  );
}
