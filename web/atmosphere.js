"use strict";

// jobfitr atmosphere — the time-of-day ambient layer. Drives the sky, the glass
// tint, the time-of-day accent, the living halo, and deep-night twinkles from the
// browser's OWN local clock (no server, no IP). The whole UI is glass over this.
// Recomputes only on the minute; all motion is gated by prefers-reduced-motion.
// Ported from design/atmosphere-study.html (the locked reference).

(function () {
  const root = document.documentElement;
  const starsLayer = document.getElementById("stars");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Keyframes from the reference skies. acc = the accent the UI borrows from that
  // light (warm by day, moonlight at night) — no static brand color.
  const KF = [
    { m: 0, top: "#0f1730", mid: "#182238", bot: "#222d47", glow: "#3c4470", acc: "#8ea3d6", gx: "50%", gy: "88%", gA: 0.2, lum: 0.06 },
    { m: 300, top: "#212c4c", mid: "#3f4266", bot: "#665c78", glow: "#9a7f88", acc: "#bd8fa2", gx: "64%", gy: "82%", gA: 0.32, lum: 0.2 },
    { m: 390, top: "#a7c0df", mid: "#eec6cd", bot: "#f7d8c7", glow: "#fff1e4", acc: "#f0a184", gx: "40%", gy: "74%", gA: 0.75, lum: 0.68 },
    { m: 540, top: "#9ec9ec", mid: "#dde3f1", bot: "#f2d4dd", glow: "#ffffff", acc: "#ec9caf", gx: "46%", gy: "40%", gA: 0.8, lum: 0.86 },
    { m: 720, top: "#a6d2f2", mid: "#d6e8f5", bot: "#eaf3ee", glow: "#ffffff", acc: "#e6b985", gx: "50%", gy: "16%", gA: 0.85, lum: 1.0 },
    { m: 900, top: "#a9cbe8", mid: "#e7dbe6", bot: "#f4dacf", glow: "#fff6ec", acc: "#edac86", gx: "56%", gy: "36%", gA: 0.8, lum: 0.88 },
    { m: 1110, top: "#82a7cf", mid: "#f0c6b6", bot: "#f4c3b2", glow: "#ffd9a6", acc: "#eaa06f", gx: "62%", gy: "66%", gA: 0.72, lum: 0.62 },
    { m: 1230, top: "#50608a", mid: "#c9a0b1", bot: "#c2b2cf", glow: "#f2b391", acc: "#d98f80", gx: "60%", gy: "78%", gA: 0.58, lum: 0.38 },
    { m: 1320, top: "#233151", mid: "#3f5a7a", bot: "#495472", glow: "#d9b48f", acc: "#6d7fb0", gx: "55%", gy: "84%", gA: 0.34, lum: 0.18 },
    { m: 1440, top: "#0f1730", mid: "#182238", bot: "#222d47", glow: "#3c4470", acc: "#8ea3d6", gx: "50%", gy: "88%", gA: 0.2, lum: 0.06 },
  ];

  const hex = (h) => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
  const lerp = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
  const cssRgb = (a) => `rgb(${Math.round(a[0])},${Math.round(a[1])},${Math.round(a[2])})`;
  const rgba = (a, al) => `rgba(${Math.round(a[0])},${Math.round(a[1])},${Math.round(a[2])},${al})`;
  const pnum = (s) => parseFloat(s);
  const WHITE = [255, 255, 255];
  const NIGHT = [16, 19, 31];

  function frame(minute) {
    const t = ((minute % 1440) + 1440) % 1440;
    let i = 0;
    for (let k = 0; k < KF.length - 1; k++) {
      if (t >= KF[k].m && t < KF[k + 1].m) { i = k; break; }
    }
    const a = KF[i], b = KF[i + 1], f = (t - a.m) / (b.m - a.m);
    return {
      top: lerp(hex(a.top), hex(b.top), f),
      mid: lerp(hex(a.mid), hex(b.mid), f),
      bot: lerp(hex(a.bot), hex(b.bot), f),
      glow: lerp(hex(a.glow), hex(b.glow), f),
      acc: lerp(hex(a.acc), hex(b.acc), f),
      gx: pnum(a.gx) + (pnum(b.gx) - pnum(a.gx)) * f,
      gy: pnum(a.gy) + (pnum(b.gy) - pnum(a.gy)) * f,
      gA: a.gA + (b.gA - a.gA) * f,
      lum: a.lum + (b.lum - a.lum) * f,
    };
  }

  let starsOn = false;
  function apply(minute) {
    const fr = frame(minute);
    root.style.setProperty("--sky-top", cssRgb(fr.top));
    root.style.setProperty("--sky-mid", cssRgb(fr.mid));
    root.style.setProperty("--sky-bot", cssRgb(fr.bot));
    root.style.setProperty("--gx", fr.gx + "%");
    root.style.setProperty("--gy", fr.gy + "%");
    root.style.setProperty("--gA", fr.gA);
    root.style.setProperty("--accent", cssRgb(fr.acc));
    root.style.setProperty("--wash-a", rgba(fr.glow, 0.24));
    root.style.setProperty("--wash-b", rgba(fr.acc, 0.24));

    const bright = fr.lum > 0.5;
    const tint = bright ? lerp(fr.mid, WHITE, 0.5) : lerp(fr.mid, NIGHT, 0.55);
    // the sun-glow flips dark WITH the glass — when the UI goes to night, so does the sun
    root.style.setProperty("--glow", cssRgb(bright ? fr.glow : lerp(fr.glow, NIGHT, 0.6)));
    const cardA = 0.3 + (1 - fr.lum) * 0.16;
    root.style.setProperty("--card-bg", rgba(tint, cardA));
    root.style.setProperty("--field-bg", rgba(tint, cardA * 0.8));
    if (bright) {
      root.style.setProperty("--ink", "#0b0f1a");
      root.style.setProperty("--sub", "rgba(11,15,26,0.60)");
      root.style.setProperty("--card-brd", "rgba(255,255,255,0.62)");
      root.style.setProperty("--rip-col", "rgba(255,255,255,0.85)"); // bright scene → light heartbeat
    } else {
      root.style.setProperty("--ink", "#f4f6fb");
      root.style.setProperty("--sub", "rgba(244,246,251,0.66)");
      root.style.setProperty("--card-brd", "rgba(255,255,255,0.18)");
      root.style.setProperty("--rip-col", "rgba(10,14,26,0.62)"); // dark scene → shadowy heartbeat
    }
    // the halo breathes slower as it darkens — the app "sleeping"
    root.style.setProperty("--breath", (4.2 + (1 - fr.lum) * 4.5).toFixed(1) + "s");
    // the heartbeat slows toward sleep as it darkens
    bpm = 46 + fr.lum * 20; // ~46 bpm deep night → ~66 bpm midday

    const wantStars = fr.lum < 0.16 && !reduce && starsLayer;
    if (wantStars && !starsOn) { starsOn = true; startStars(); }
    else if (!wantStars && starsOn) { starsOn = false; stopStars(); }
  }

  // ── deep-night twinkles ─────────────────────────────────────────────────────
  let starTimer = null;
  function spawnStar() {
    const n = 1 + Math.floor(Math.random() * 2);
    for (let i = 0; i < n; i++) {
      const s = document.createElement("div");
      s.className = "tw on";
      s.style.left = Math.random() * 100 + "%";
      s.style.top = Math.random() * 72 + "%";
      const px = (1.5 + Math.random() * 1.8).toFixed(1) + "px";
      s.style.width = s.style.height = px;
      starsLayer.appendChild(s);
      setTimeout(() => s.remove(), 2700);
    }
  }
  function startStars() { if (!starTimer) starTimer = setInterval(spawnStar, 620); }
  function stopStars() {
    if (starTimer) { clearInterval(starTimer); starTimer = null; }
    if (starsLayer) starsLayer.textContent = "";
  }

  // ── heartbeat: a faint lub-dub pulse from the chat, slowing toward sleep ─────
  const pulse = document.getElementById("pulse");
  let bpm = 62; // set by time-of-day in apply()
  function rip(dub) {
    // Skip while hidden: backgrounded tabs pause CSS animations, so animationend
    // would never fire and rips would pile up. No point beating when unseen.
    if (reduce || !pulse || document.hidden) return;
    const r = document.createElement("div");
    r.className = "rip run" + (dub ? " dub" : "");
    pulse.appendChild(r);
    const kill = () => r.remove();
    r.addEventListener("animationend", kill);
    setTimeout(kill, 2000); // fallback: a dropped animationend can't leak
  }
  function heartbeat() {
    rip(false); // lub
    setTimeout(() => rip(true), 235); // dub, a beat later
    setTimeout(heartbeat, 60000 / bpm); // rest, then the next beat
  }

  // ── boot on the local clock, tick each minute ───────────────────────────────
  function nowMinute() {
    const d = new Date();
    return d.getHours() * 60 + d.getMinutes();
  }
  apply(nowMinute());
  setInterval(() => apply(nowMinute()), 15000);
  if (!reduce && pulse) setTimeout(heartbeat, 600);
})();
