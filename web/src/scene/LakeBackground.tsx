import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import type { Theme } from "../types";

const MAX_RIPPLES = 16;

const vertexShader = /* glsl */ `
  precision highp float;

  uniform float uTime;
  uniform vec4 uRipples[${MAX_RIPPLES}];
  varying vec3 vWorld;
  varying float vWave;
  varying float vAmbient;
  varying float vRipple;

  float ambientWave(vec2 p) {
    float broad = sin(p.x * 0.46 + uTime * 0.17) * 0.055;
    broad += cos(p.y * 0.38 - uTime * 0.13) * 0.048;
    broad += sin((p.x + p.y) * 0.82 + uTime * 0.11) * 0.025;
    float tremor = sin(p.x * 5.7 + p.y * 4.2 + uTime * 0.62) * 0.007;
    tremor += cos(p.x * 8.1 - p.y * 6.4 - uTime * 0.51) * 0.005;
    return broad + tremor;
  }

  float rippleWave(vec2 p) {
    float sum = 0.0;
    for (int i = 0; i < ${MAX_RIPPLES}; i++) {
      vec4 ripple = uRipples[i];
      float age = uTime - ripple.z;
      float distanceFromCenter = distance(p, ripple.xy);
      float rippleActive = step(0.0, age) * (1.0 - smoothstep(3.3, 5.2, age));
      float ring = sin(distanceFromCenter * 12.5 - age * 5.4);
      float envelope = exp(-distanceFromCenter * 0.7) * exp(-age * 0.7);
      sum += ring * envelope * ripple.w * rippleActive;
    }
    return sum;
  }

  void main() {
    vec3 p = position;
    float ambient = ambientWave(p.xz);
    float ripple = rippleWave(p.xz);
    float height = ambient + ripple;
    p.y += height;
    vWave = height;
    vAmbient = ambient;
    vRipple = ripple;
    vWorld = (modelMatrix * vec4(p, 1.0)).xyz;
    gl_Position = projectionMatrix * viewMatrix * vec4(vWorld, 1.0);
  }
`;

const fragmentShader = /* glsl */ `
  precision highp float;

  uniform float uTime;
  uniform vec3 uDeep;
  uniform vec3 uShallow;
  uniform vec3 uGlint;
  uniform vec3 uRippleColor;
  varying vec3 vWorld;
  varying float vWave;
  varying float vAmbient;
  varying float vRipple;

  float hash(vec2 p) {
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
  }

  void main() {
    vec3 dx = dFdx(vWorld);
    vec3 dy = dFdy(vWorld);
    vec3 normal = normalize(cross(dx, dy));
    if (normal.y < 0.0) normal *= -1.0;

    float calmLight = clamp(0.58 + normal.x * 0.34 + normal.z * 0.20, 0.0, 1.0);
    float ambientTone = clamp(0.50 + vAmbient * 3.6, 0.0, 1.0);
    float depthMix = clamp(calmLight * 0.58 + ambientTone * 0.42 + vWave * 0.72, 0.0, 1.0);
    vec3 color = mix(uDeep, uShallow, depthMix);

    // Carry the actual ripple field into color. This keeps rings readable from
    // a strict top-down camera even when their geometry occupies few pixels.
    float rippleBand = smoothstep(0.008, 0.085, abs(vRipple));
    float rippleCrest = smoothstep(0.012, 0.12, vRipple);
    float rippleTrough = smoothstep(0.012, 0.12, -vRipple);
    color = mix(color, uRippleColor, rippleBand * 0.10 + rippleCrest * 0.15);
    color = mix(color, uDeep, rippleTrough * 0.14);

    vec3 lightDirection = normalize(vec3(-0.22, 1.0, 0.18));
    float specular = pow(max(dot(normal, lightDirection), 0.0), 72.0);
    float grain = hash(floor(vWorld.xz * 22.0) + floor(uTime * 1.2));
    float brokenLight = specular * smoothstep(0.63, 0.98, grain);
    float microGlint = smoothstep(0.077, 0.105, abs(vWave)) * smoothstep(0.78, 0.99, grain) * 0.24;
    float rippleSheen = rippleCrest * (0.055 + grain * 0.055);
    color += uGlint * (brokenLight * 0.9 + microGlint + rippleSheen);

    gl_FragColor = vec4(color, 1.0);
    #include <tonemapping_fragment>
    #include <colorspace_fragment>
  }
`;

function useReducedMotion() {
  const [reduced, setReduced] = useState(() =>
    typeof window === "undefined" ? true : window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );

  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return reduced;
}

interface LakeBackgroundProps {
  theme: Theme;
}

export function LakeBackground({ theme }: LakeBackgroundProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  const reducedMotion = useReducedMotion();
  const [fallback, setFallback] = useState(false);

  useEffect(() => {
    if (reducedMotion || fallback || !mountRef.current) return;

    const host = mountRef.current;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({
        alpha: false,
        antialias: (navigator.hardwareConcurrency ?? 4) > 4,
        powerPreference: "low-power",
      });
    } catch {
      setFallback(true);
      return;
    }

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(43, 1, 0.1, 40);
    camera.position.set(0, 7, 0);
    camera.up.set(0, 0, -1);
    camera.lookAt(0, 0, 0);

    const lowPower =
      (navigator.hardwareConcurrency ?? 4) <= 2 ||
      ((navigator as Navigator & { deviceMemory?: number }).deviceMemory ?? 4) <= 2;
    const segments = lowPower ? 56 : 104;
    const geometry = new THREE.PlaneGeometry(24, 24, segments, segments);
    geometry.rotateX(-Math.PI / 2);

    const rippleUniforms = Array.from(
      { length: MAX_RIPPLES },
      () => new THREE.Vector4(0, 0, -1000, 0),
    );
    const palette =
      theme === "dark"
        ? { deep: 0x041a25, shallow: 0x155064, glint: 0x9bcac6, ripple: 0x4fa4b1 }
        : { deep: 0x5f9fb5, shallow: 0xc6dfe1, glint: 0xffefb9, ripple: 0x7dc7d2 };

    const material = new THREE.ShaderMaterial({
      vertexShader,
      fragmentShader,
      uniforms: {
        uTime: { value: 0 },
        uRipples: { value: rippleUniforms },
        uDeep: { value: new THREE.Color(palette.deep) },
        uShallow: { value: new THREE.Color(palette.shallow) },
        uGlint: { value: new THREE.Color(palette.glint) },
        uRippleColor: { value: new THREE.Color(palette.ripple) },
      },
      side: THREE.DoubleSide,
    });
    const water = new THREE.Mesh(geometry, material);
    scene.add(water);

    renderer.setPixelRatio(Math.min(window.devicePixelRatio, lowPower ? 1 : 1.5));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.domElement.className = "lake-canvas";
    renderer.domElement.setAttribute("aria-hidden", "true");
    host.appendChild(renderer.domElement);

    const clock = new THREE.Clock();
    const raycaster = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    const waterPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    const worldPoint = new THREE.Vector3();
    let rippleCursor = 0;
    let animationFrame = 0;
    let pointerDown = false;
    let lastTrailX = -100;
    let lastTrailY = -100;
    let lastTrailTime = 0;

    const resize = () => {
      const width = host.clientWidth || window.innerWidth;
      const height = host.clientHeight || window.innerHeight;
      renderer.setSize(width, height, false);
      camera.aspect = width / Math.max(1, height);
      camera.updateProjectionMatrix();
    };

    const addRipple = (clientX: number, clientY: number, strength: number) => {
      ndc.set((clientX / window.innerWidth) * 2 - 1, -(clientY / window.innerHeight) * 2 + 1);
      raycaster.setFromCamera(ndc, camera);
      if (!raycaster.ray.intersectPlane(waterPlane, worldPoint)) return;
      rippleUniforms[rippleCursor].set(
        worldPoint.x,
        worldPoint.z,
        material.uniforms.uTime.value,
        strength,
      );
      rippleCursor = (rippleCursor + 1) % MAX_RIPPLES;
    };

    const onPointerMove = (event: PointerEvent) => {
      const now = performance.now();
      const distance = Math.hypot(event.clientX - lastTrailX, event.clientY - lastTrailY);
      if (distance < (pointerDown ? 13 : 26) || now - lastTrailTime < (pointerDown ? 35 : 70)) return;
      addRipple(event.clientX, event.clientY, pointerDown ? 0.13 : 0.065);
      lastTrailX = event.clientX;
      lastTrailY = event.clientY;
      lastTrailTime = now;
    };
    const onPointerDown = (event: PointerEvent) => {
      pointerDown = true;
      addRipple(event.clientX, event.clientY, 0.20);
    };
    const onPointerUp = () => {
      pointerDown = false;
    };
    const onClick = (event: MouseEvent) => addRipple(event.clientX, event.clientY, 0.16);
    const onContextLost = (event: Event) => {
      event.preventDefault();
      setFallback(true);
    };

    const draw = () => {
      material.uniforms.uTime.value = clock.getElapsedTime();
      renderer.render(scene, camera);
      animationFrame = requestAnimationFrame(draw);
    };
    const onVisibility = () => {
      cancelAnimationFrame(animationFrame);
      if (!document.hidden) {
        clock.getDelta();
        animationFrame = requestAnimationFrame(draw);
      }
    };

    resize();
    draw();
    window.addEventListener("resize", resize);
    window.addEventListener("pointermove", onPointerMove, { passive: true });
    window.addEventListener("pointerdown", onPointerDown, { passive: true });
    window.addEventListener("pointerup", onPointerUp, { passive: true });
    window.addEventListener("pointercancel", onPointerUp, { passive: true });
    window.addEventListener("click", onClick, { passive: true });
    document.addEventListener("visibilitychange", onVisibility);
    renderer.domElement.addEventListener("webglcontextlost", onContextLost);

    return () => {
      cancelAnimationFrame(animationFrame);
      window.removeEventListener("resize", resize);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("pointercancel", onPointerUp);
      window.removeEventListener("click", onClick);
      document.removeEventListener("visibilitychange", onVisibility);
      renderer.domElement.removeEventListener("webglcontextlost", onContextLost);
      geometry.dispose();
      material.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, [fallback, reducedMotion, theme]);

  return (
    <div
      ref={mountRef}
      className={`lake-background${fallback || reducedMotion ? " lake-background--static" : ""}`}
      aria-hidden="true"
      data-testid="lake-background"
    />
  );
}
