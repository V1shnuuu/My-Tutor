import { useEffect, useRef } from "react";
import type { Viseme } from "../lib/visemes";
import type { AvatarState, RendererProps } from "./avatarTypes";

// Mouth targets per viseme: width, openness, rounding, teeth visible, tongue visible.
const SHAPES: Record<Viseme, [number, number, number, number, number]> = {
  sil: [0.5, 0.04, 0, 0, 0],
  PP: [0.38, 0.0, 0.1, 0, 0],
  FF: [0.55, 0.12, 0, 1, 0],
  TH: [0.5, 0.26, 0, 1, 1],
  DD: [0.55, 0.3, 0, 1, 0],
  kk: [0.5, 0.36, 0.1, 0, 0],
  CH: [0.42, 0.3, 0.5, 0, 0],
  SS: [0.66, 0.14, 0, 1, 0],
  nn: [0.5, 0.2, 0, 1, 0.5],
  RR: [0.42, 0.3, 0.55, 0, 0],
  aa: [0.6, 0.95, 0.05, 0, 0],
  E: [0.76, 0.45, 0, 1, 0],
  I: [0.86, 0.28, 0, 1, 0],
  O: [0.3, 0.72, 1, 0, 0],
  U: [0.22, 0.42, 1, 0, 0],
};

/**
 * 2D renderer for the shared viseme timeline. Draws with refs inside one rAF loop so React
 * never re-renders per frame. The same `getViseme` clock drives the 3D GLB in Avatar3D.
 * Also the fallback whenever WebGL or the GLB is unavailable.
 */
export default function Avatar2D({ state, getViseme, label, stateLabel }: RendererProps) {
  const mouthRef = useRef<SVGPathElement>(null);
  const innerRef = useRef<SVGPathElement>(null);
  const teethRef = useRef<SVGRectElement>(null);
  const tongueRef = useRef<SVGEllipseElement>(null);
  const headRef = useRef<SVGGElement>(null);
  const lidsRef = useRef<SVGGElement>(null);
  const browL = useRef<SVGPathElement>(null);
  const browR = useRef<SVGPathElement>(null);
  const pupils = useRef<SVGGElement>(null);
  const stateRef = useRef<AvatarState>(state);
  stateRef.current = state;

  useEffect(() => {
    let raf = 0;
    const cur = { w: 0.5, h: 0.04, r: 0, teeth: 0, tongue: 0, nod: 0, browY: 0, tilt: 0, eyeX: 0, eyeY: 0 };
    let nextBlink = performance.now() + 2500;
    let blinkUntil = 0;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const lerp = (a: number, b: number, k: number) => a + (b - a) * k;

    const frame = (now: number) => {
      const st = stateRef.current;
      const { v, w } = st === "speaking" ? getViseme() : { v: "sil" as Viseme, w: 0 };
      const [tw, th, tr, tteeth, ttongue] = SHAPES[v];
      const amp = st === "speaking" ? 0.35 + 0.65 * w : 1;
      // attack faster than release so consonants read crisply
      const k = th > cur.h ? 0.45 : 0.25;
      cur.w = lerp(cur.w, tw, 0.35);
      cur.h = lerp(cur.h, th * amp, k);
      cur.r = lerp(cur.r, tr, 0.3);
      cur.teeth = lerp(cur.teeth, tteeth, 0.3);
      cur.tongue = lerp(cur.tongue, ttongue, 0.3);

      // Mouth geometry (viewBox 200x200, mouth centred at 100,132)
      const W = 22 + 30 * cur.w - 10 * cur.r;
      const H = 1.5 + 30 * cur.h;
      const cx = 100, cy = 132;
      const lipR = 1 + cur.r * 0.6; // rounding pulls corners in and lips out
      const path = `M ${cx - W} ${cy} Q ${cx - W * 0.5} ${cy - H * 0.9 * lipR} ${cx} ${cy - H * 0.95}
        Q ${cx + W * 0.5} ${cy - H * 0.9 * lipR} ${cx + W} ${cy}
        Q ${cx + W * 0.5} ${cy + H * 1.1 * lipR} ${cx} ${cy + H * 1.05}
        Q ${cx - W * 0.5} ${cy + H * 1.1 * lipR} ${cx - W} ${cy} Z`;
      mouthRef.current?.setAttribute("d", path);
      innerRef.current?.setAttribute("d", path);
      if (teethRef.current) {
        teethRef.current.setAttribute("x", String(cx - W * 0.7));
        teethRef.current.setAttribute("width", String(W * 1.4));
        teethRef.current.setAttribute("y", String(cy - H * 0.9));
        teethRef.current.setAttribute("height", String(Math.max(0, Math.min(6, H * 0.5)) * cur.teeth));
      }
      if (tongueRef.current) {
        tongueRef.current.setAttribute("cy", String(cy + H * 0.6));
        tongueRef.current.setAttribute("rx", String(W * 0.45 * cur.tongue));
        tongueRef.current.setAttribute("ry", String(Math.max(0, H * 0.35) * cur.tongue));
      }

      // Head motion: idle sway, listening tilt, speaking nods on amplitude peaks
      const t = now / 1000;
      let ty = 0, tx = 0, tiltT = 0, browT = 0, ex = 0, ey = 0;
      if (!reduced) {
        ty = Math.sin(t * 1.1) * 1.2;
        tx = Math.sin(t * 0.7) * 0.8;
        if (st === "listening") { tiltT = -6; browT = -4; }
        if (st === "thinking") { ex = -4; ey = -4; browT = 2; tiltT = 3; }
        if (st === "speaking") { cur.nod = lerp(cur.nod, cur.h * 2.2, 0.2); ty += cur.nod; }
        if (st === "muted") browT = 1;
      }
      cur.tilt = lerp(cur.tilt, tiltT, 0.08);
      cur.browY = lerp(cur.browY, browT, 0.12);
      cur.eyeX = lerp(cur.eyeX, ex, 0.12);
      cur.eyeY = lerp(cur.eyeY, ey, 0.12);
      headRef.current?.setAttribute("transform", `translate(${tx} ${ty}) rotate(${cur.tilt} 100 100)`);
      browL.current?.setAttribute("transform", `translate(0 ${cur.browY})`);
      browR.current?.setAttribute("transform", `translate(0 ${cur.browY})`);
      pupils.current?.setAttribute("transform", `translate(${cur.eyeX} ${cur.eyeY})`);

      // Blink
      if (now > nextBlink) { blinkUntil = now + 120; nextBlink = now + 2500 + Math.random() * 3500; }
      const blinking = now < blinkUntil;
      lidsRef.current?.setAttribute("opacity", blinking ? "1" : "0");

      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [getViseme]);

  return (
    <svg viewBox="0 0 200 200" role="img" aria-label={`${label}: ${stateLabel}`}>
        <defs>
          <clipPath id="mouthClip"><path ref={innerRef} d="M 70 132 L 130 132 Z" /></clipPath>
        </defs>
        <g ref={headRef}>
          {/* shoulders */}
          <path d="M 30 200 C 40 160 70 156 100 156 C 130 156 160 160 170 200 Z" fill="var(--accent)" opacity="0.9" />
          <path d="M 30 200 C 40 160 70 156 100 156 C 130 156 160 160 170 200" fill="none" stroke="var(--ink)" strokeWidth="2.2" />
          {/* neck */}
          <path d="M 88 140 L 88 160 L 112 160 L 112 140" fill="var(--paper)" stroke="var(--ink)" strokeWidth="2" />
          {/* head */}
          <ellipse cx="100" cy="100" rx="52" ry="58" fill="var(--paper)" stroke="var(--ink)" strokeWidth="2.4" />
          {/* hair */}
          <path d="M 48 92 C 46 50 80 34 104 38 C 132 40 154 58 152 92 C 146 78 134 66 120 64 C 104 62 96 68 84 66 C 70 64 58 76 48 92 Z" fill="var(--ink)" />
          {/* ears */}
          <ellipse cx="47" cy="104" rx="6" ry="9" fill="var(--paper)" stroke="var(--ink)" strokeWidth="2" />
          <ellipse cx="153" cy="104" rx="6" ry="9" fill="var(--paper)" stroke="var(--ink)" strokeWidth="2" />
          {/* brows */}
          <path ref={browL} d="M 68 84 Q 80 78 92 83" fill="none" stroke="var(--ink)" strokeWidth="2.6" strokeLinecap="round" />
          <path ref={browR} d="M 108 83 Q 120 78 132 84" fill="none" stroke="var(--ink)" strokeWidth="2.6" strokeLinecap="round" />
          {/* eyes */}
          <ellipse cx="80" cy="98" rx="9" ry="9.5" fill="#fff" stroke="var(--ink)" strokeWidth="2" />
          <ellipse cx="120" cy="98" rx="9" ry="9.5" fill="#fff" stroke="var(--ink)" strokeWidth="2" />
          <g ref={pupils}>
            <circle cx="81" cy="99" r="4" fill="var(--ink)" />
            <circle cx="121" cy="99" r="4" fill="var(--ink)" />
            <circle cx="82.5" cy="97.5" r="1.2" fill="#fff" />
            <circle cx="122.5" cy="97.5" r="1.2" fill="#fff" />
          </g>
          <g ref={lidsRef} opacity="0">
            <ellipse cx="80" cy="98" rx="9.6" ry="10" fill="var(--paper)" stroke="var(--ink)" strokeWidth="2" />
            <ellipse cx="120" cy="98" rx="9.6" ry="10" fill="var(--paper)" stroke="var(--ink)" strokeWidth="2" />
          </g>
          {/* glasses */}
          <g fill="none" stroke="var(--ink-2)" strokeWidth="1.6" opacity="0.8">
            <rect x="67" y="87" width="26" height="22" rx="8" />
            <rect x="107" y="87" width="26" height="22" rx="8" />
            <path d="M 93 97 L 107 97 M 67 96 L 52 92 M 133 96 L 148 92" />
          </g>
          {/* nose */}
          <path d="M 100 104 Q 96 116 102 118" fill="none" stroke="var(--ink)" strokeWidth="2" strokeLinecap="round" />
          {/* mouth */}
          <path ref={mouthRef} d="M 70 132 L 130 132 Z" fill="#3a1d24" stroke="var(--ink)" strokeWidth="2.2" strokeLinejoin="round" />
          <g clipPath="url(#mouthClip)">
            <rect ref={teethRef} x="80" y="128" width="40" height="0" fill="#fff" />
            <ellipse ref={tongueRef} cx="100" cy="140" rx="0" ry="0" fill="#c85c6a" />
          </g>
        </g>
    </svg>
  );
}
