import { useEffect, useRef } from "react";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import type { Viseme } from "../lib/visemes";
import type { AvatarState, RendererProps } from "./avatarTypes";

interface Props extends RendererProps {
  /** Called once the GLB is on screen, so the wrapper can retire the 2D stand-in. */
  onReady: () => void;
  /** Called when WebGL or the GLB is unavailable, so the wrapper falls back to 2D. */
  onFail: () => void;
}

const MODEL_URL = "/avatar/tutor.glb";

// Oculus visemes, identical to the set `visemes.ts` emits and to the GLB's morph names.
// `sil` is omitted: the rest pose is already a closed mouth, so silence = all weights 0.
const SPOKEN: Exclude<Viseme, "sil">[] =
  ["PP", "FF", "TH", "DD", "kk", "CH", "SS", "nn", "RR", "aa", "E", "I", "O", "U"];

// Head-and-shoulders framing, in metres relative to the head bone.
const CAM_OFFSET = new THREE.Vector3(0, 0.045, 0.70);
const LOOK_OFFSET = new THREE.Vector3(0, 0.015, 0);

/**
 * 3D renderer: a Ready Player Me humanoid GLB whose `viseme_*` morph targets are driven by
 * the same `getViseme` clock as the 2D face, so lip-sync stays anchored to the speech audio.
 * Everything animates inside one rAF loop against refs — React never re-renders per frame.
 */
export default function Avatar3D({ state, getViseme, label, stateLabel, onReady, onFail }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef<AvatarState>(state);
  stateRef.current = state;
  // Effect callbacks must not re-run the whole scene when the parent re-renders.
  const cbRef = useRef({ getViseme, onReady, onFail });
  cbRef.current = { getViseme, onReady, onFail };

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    } catch {
      cbRef.current.onFail();
      return;
    }

    let disposed = false;
    let raf = 0;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.setSize(host.clientWidth || 1, host.clientHeight || 1, false);
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    host.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(28, 1, 0.05, 20);

    // Soft studio reflections so the PBR skin/outfit does not read flat.
    const pmrem = new THREE.PMREMGenerator(renderer);
    const envRT = pmrem.fromScene(new RoomEnvironment(), 0.04);
    scene.environment = envRT.texture;

    const key = new THREE.DirectionalLight(0xffffff, 2.1);
    key.position.set(-0.6, 1.9, 1.4);
    const fill = new THREE.DirectionalLight(0xdfe8ff, 0.7);
    fill.position.set(1.2, 1.2, 0.8);
    const rim = new THREE.DirectionalLight(0xffffff, 1.5);
    rim.position.set(0.3, 1.7, -1.6);
    scene.add(key, fill, rim, new THREE.HemisphereLight(0xffffff, 0x8d8577, 0.55));

    // ---- rig handles, filled in on load -------------------------------------------------
    type MorphMesh = { dict: Record<string, number>; infl: number[] };
    const morphs: MorphMesh[] = [];
    let head: THREE.Object3D | null = null;
    let neck: THREE.Object3D | null = null;
    let spine: THREE.Object3D | null = null;
    const rest = new WeakMap<THREE.Object3D, THREE.Euler>();

    const setMorph = (name: string, v: number) => {
      for (const m of morphs) {
        const i = m.dict[name];
        if (i !== undefined) m.infl[i] = v;
      }
    };

    const onLost = (e: Event) => { e.preventDefault(); cbRef.current.onFail(); };
    renderer.domElement.addEventListener("webglcontextlost", onLost);

    const ro = new ResizeObserver(() => {
      const w = host.clientWidth, h = host.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    });
    ro.observe(host);

    // Fetched rather than handed to loader.load() so a short read is caught: a truncated GLB
    // still parses (the tail holds only later textures) and would silently render untextured.
    const loadModel = async () => {
      const res = await fetch(MODEL_URL);
      if (!res.ok) throw new Error(`glb ${res.status}`);
      const buf = await res.arrayBuffer();
      const declared = new DataView(buf).getUint32(8, true);
      if (buf.byteLength !== declared) throw new Error(`glb truncated: ${buf.byteLength}/${declared}`);
      return await new GLTFLoader().parseAsync(buf, "");
    };

    loadModel().then(
      (gltf) => {
        if (disposed) return;
        const root = gltf.scene;
        root.traverse((o) => {
          const mesh = o as THREE.Mesh;
          if (mesh.isMesh) {
            mesh.frustumCulled = false;
            if (mesh.morphTargetDictionary && mesh.morphTargetInfluences) {
              morphs.push({ dict: mesh.morphTargetDictionary, infl: mesh.morphTargetInfluences });
            }
          }
        });
        scene.add(root);
        root.updateMatrixWorld(true);

        head = root.getObjectByName("Head") ?? null;
        neck = root.getObjectByName("Neck") ?? null;
        spine = root.getObjectByName("Spine2") ?? null;
        for (const b of [head, neck, spine]) if (b) rest.set(b, b.rotation.clone());

        // Frame on the head bone so the shot holds for any avatar height.
        const anchor = new THREE.Vector3();
        if (head) head.getWorldPosition(anchor);
        else new THREE.Box3().setFromObject(root).getCenter(anchor);
        camera.position.copy(anchor).add(CAM_OFFSET);
        camera.lookAt(anchor.clone().add(LOOK_OFFSET));

        cbRef.current.onReady();
      },
      (err) => {
        if (disposed) return;
        console.error("Avatar3D: falling back to the 2D face —", err);
        cbRef.current.onFail();
      },
    );

    // ---- animation ----------------------------------------------------------------------
    const cur = new Float32Array(SPOKEN.length);
    const extra = { blink: 0, browUp: 0, browDown: 0, smile: 0, lookUp: 0, lookOut: 0, nod: 0, tilt: 0, sway: 0 };
    let nextBlink = performance.now() + 2500;
    let blinkUntil = 0;
    const lerp = (a: number, b: number, k: number) => a + (b - a) * k;

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      if (document.hidden) return;
      const st = stateRef.current;

      // Visemes: active target rises fast, everything else relaxes slower — crisp consonants.
      const { v, w } = st === "speaking" ? cbRef.current.getViseme() : { v: "sil" as Viseme, w: 0 };
      const amp = 0.35 + 0.65 * w;
      let open = 0;
      for (let i = 0; i < SPOKEN.length; i++) {
        const target = SPOKEN[i] === v ? amp : 0;
        cur[i] = lerp(cur[i], target, target > cur[i] ? 0.45 : 0.25);
        if (cur[i] > 0.01 || cur[i] < -0.01) setMorph(`viseme_${SPOKEN[i]}`, cur[i]);
        open = Math.max(open, cur[i]);
      }

      // Expression per state.
      const speaking = st === "speaking";
      extra.blink = lerp(extra.blink, now < blinkUntil ? 1 : 0, now < blinkUntil ? 0.85 : 0.4);
      extra.browUp = lerp(extra.browUp, st === "listening" ? 0.38 : speaking ? 0.10 + 0.22 * open : 0.05, 0.10);
      extra.browDown = lerp(extra.browDown, st === "thinking" ? 0.30 : 0, 0.10);
      extra.smile = lerp(extra.smile, st === "muted" ? 0.05 : speaking ? 0.08 : 0.20, 0.06);
      extra.lookUp = lerp(extra.lookUp, st === "thinking" ? 0.35 : 0, 0.08);
      extra.lookOut = lerp(extra.lookOut, st === "thinking" ? 0.30 : 0, 0.08);

      setMorph("eyeBlinkLeft", extra.blink);
      setMorph("eyeBlinkRight", extra.blink);
      setMorph("browInnerUp", extra.browUp);
      setMorph("browDownLeft", extra.browDown);
      setMorph("browDownRight", extra.browDown);
      setMorph("mouthSmileLeft", extra.smile);
      setMorph("mouthSmileRight", extra.smile);
      setMorph("eyeLookUpLeft", extra.lookUp);
      setMorph("eyeLookUpRight", extra.lookUp);
      setMorph("eyeLookOutLeft", extra.lookOut);
      setMorph("eyeLookInRight", extra.lookOut);

      if (now > nextBlink) { blinkUntil = now + 110; nextBlink = now + 2500 + Math.random() * 3500; }

      // Head motion: idle sway, listening tilt, nods riding the speech amplitude.
      if (!reduced && head) {
        const t = now / 1000;
        extra.nod = lerp(extra.nod, speaking ? open * 0.06 : 0, 0.2);
        extra.tilt = lerp(extra.tilt, st === "listening" ? 0.10 : st === "thinking" ? -0.06 : 0, 0.06);
        extra.sway = lerp(extra.sway, Math.sin(t * 0.7) * 0.035, 0.1);
        const pitch = Math.sin(t * 1.1) * 0.012 + extra.nod;
        for (const [bone, k] of [[head, 1], [neck, 0.45], [spine, 0.2]] as const) {
          const r = bone && rest.get(bone);
          if (!bone || !r) continue;
          bone.rotation.set(r.x + pitch * k, r.y + extra.sway * k, r.z + extra.tilt * k);
        }
      }

      renderer.render(scene, camera);
    };
    raf = requestAnimationFrame(frame);

    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      ro.disconnect();
      renderer.domElement.removeEventListener("webglcontextlost", onLost);
      scene.traverse((o) => {
        const mesh = o as THREE.Mesh;
        if (!mesh.isMesh) return;
        mesh.geometry?.dispose();
        for (const mat of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
          if (!mat) continue;
          for (const val of Object.values(mat)) {
            if (val && (val as THREE.Texture).isTexture) (val as THREE.Texture).dispose();
          }
          mat.dispose();
        }
      });
      envRT.texture.dispose();
      pmrem.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, []);

  return <div className="avatar-3d" ref={hostRef} role="img" aria-label={`${label}: ${stateLabel}`} />;
}
