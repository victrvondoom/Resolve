/* RESOLVE "creamy bubbles": hero scene of large soft inflated forms + scroll reveal, shrinking nav, drawn connector line.
   No libraries (the app is offline-first). The scene is a Canvas2D renderer that shades each sphere with a physically
   motivated radial gradient (key light top-left, cool fill bottom-right, specular blob, soft contact shadow).
   It is NOT WebGL; Canvas2D is used so it works on every tier and needs no assets.
   Tiering: high tier = full scene; low tier (few cores / low memory / save-data / narrow phone) or reduced motion = CSS bubbles. */
(function () {
  'use strict';
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var nav = navigator;
  var lowTier = (nav.hardwareConcurrency && nav.hardwareConcurrency <= 2) || (nav.deviceMemory && nav.deviceMemory <= 2) ||
    (nav.connection && nav.connection.saveData);

  /* ---------- nav shrinks on scroll ---------- */
  var header = document.querySelector('.site-header');
  function onScroll() { if (header) header.classList.toggle('shrunk', window.scrollY > 40); }
  window.addEventListener('scroll', onScroll, { passive: true }); onScroll();

  /* ---------- scroll reveal (once, 80ms stagger) ---------- */
  var sel = '.band .eyebrow,.band h2,.section-lede,.cards .card,.pipeline li,.try-steps li,.table-card,.status-card,.cta-inner,.hero-copy>*';
  var items = Array.prototype.slice.call(document.querySelectorAll(sel));
  if (reduce || !('IntersectionObserver' in window)) { /* leave everything visible */ }
  else {
    var io = new IntersectionObserver(function (es) {
      var order = 0;
      es.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.style.setProperty('--rd', (order++ * 80) + 'ms');
        e.target.classList.add('in'); io.unobserve(e.target);
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -6% 0px' });
    items.forEach(function (el) { el.classList.add('reveal'); io.observe(el); });
  }

  /* ---------- idle float on cards (random period/delay), disabled for reduced motion ---------- */
  if (!reduce) {
    document.querySelectorAll('.status-card,.try-steps li').forEach(function (el, i) {
      el.style.setProperty('--dur', (6 + (i * 37 % 30) / 10) + 's');
      el.style.setProperty('--delay', (-(i * 53 % 60) / 10) + 's');
      el.classList.add('float');
    });
  }

  /* ---------- how-it-works: soft connector that draws on scroll ---------- */
  var pipe = document.querySelector('.pipeline');
  if (pipe && 'ResizeObserver' in window) {
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg'); svg.setAttribute('class', 'pipe-line'); svg.setAttribute('aria-hidden', 'true');
    var path = document.createElementNS(NS, 'path'); svg.appendChild(path); pipe.insertBefore(svg, pipe.firstChild);
    var len = 0;
    var layout = function () {
      var steps = pipe.querySelectorAll('.step'), pr = pipe.getBoundingClientRect(), d = '';
      steps.forEach(function (s, i) {
        var r = s.getBoundingClientRect(), x = r.left - pr.left + r.width / 2, y = r.top - pr.top + r.height / 2;
        if (!i) d = 'M' + x + ' ' + y;
        else { var px = steps[i - 1].getBoundingClientRect(); var ax = px.left - pr.left + px.width / 2, ay = px.top - pr.top + px.height / 2;
          d += ' C' + (ax + (x - ax) * 0.5) + ' ' + (ay + 60) + ' ' + (x - (x - ax) * 0.5) + ' ' + (y - 60) + ' ' + x + ' ' + y; }
      });
      path.setAttribute('d', d); len = path.getTotalLength ? path.getTotalLength() : 0;
      path.style.strokeDasharray = len; draw();
    };
    var draw = function () {
      if (!len) return;
      if (reduce) { path.style.strokeDashoffset = 0; return; }
      var r = pipe.getBoundingClientRect(), vh = window.innerHeight;
      var p = Math.min(1, Math.max(0, (vh * 0.85 - r.top) / (r.height + vh * 0.3)));
      path.style.strokeDashoffset = len * (1 - p);
    };
    new ResizeObserver(layout).observe(pipe); window.addEventListener('scroll', draw, { passive: true }); layout();
  }

  /* ---------- hero bubble scene ---------- */
  var hero = document.querySelector('.hero');
  if (!hero) return;
  var fb = document.createElement('div'); fb.className = 'bubble-fallback'; fb.setAttribute('aria-hidden', 'true');
  fb.innerHTML = '<i></i><i></i><i></i><i></i><i></i>'; hero.insertBefore(fb, hero.firstChild);
  var cv = document.createElement('canvas'); cv.className = 'bubbles'; cv.setAttribute('aria-hidden', 'true'); cv.dataset.renderer = 'pending';
  hero.insertBefore(cv, hero.firstChild);
  function useCss(why) { hero.classList.add('bubbles-css'); cv.dataset.renderer = 'css'; cv.dataset.reason = why; }
  var ctx = cv.getContext && cv.getContext('2d');
  if (!ctx) return useCss('no 2d context');
  if (reduce) return useCss('reduced motion');
  if (lowTier) return useCss('low tier');

  var seed = 11; function rnd() { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; }
  // positions/radii are fractions of the shorter canvas side so big forms get cropped off-screen on narrow screens
  var TONES = [[244, 236, 220], [255, 207, 176], [207, 224, 242], [255, 231, 163], [207, 227, 207]];
  var big = [
    { x: .78, y: .42, r: .46, t: 0, z: 1.0, ph: 0 }, { x: .98, y: .86, r: .30, t: 1, z: .9, ph: 1.7 },
    { x: .56, y: .90, r: .22, t: 2, z: .85, ph: 3.1 }, { x: .90, y: .06, r: .16, t: 3, z: .8, ph: 4.4 },
    { x: .05, y: .96, r: .20, t: 4, z: .7, ph: 2.2 }
  ];
  var sats = [];
  for (var i = 0; i < 16; i++) sats.push({ x: rnd(), y: rnd(), r: .018 + rnd() * .045, t: Math.floor(rnd() * 5), z: .25 + rnd() * .6, ph: rnd() * 6.28, sp: .15 + rnd() * .3 });
  var squish = {};              // index -> {t0} hover spring
  var W = 1, H = 1, dpr = 1, mx = 0, my = 0, tx = 0, ty = 0, visible = true, raf = 0, frames = 0, t0 = performance.now();

  function resize() {
    dpr = Math.min(1.75, Math.max(1, window.devicePixelRatio || 1));
    W = Math.max(1, Math.floor(cv.clientWidth * dpr)); H = Math.max(1, Math.floor(cv.clientHeight * dpr));
    if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
  }
  function rgb(c, k) { return 'rgb(' + Math.round(c[0] * k) + ',' + Math.round(c[1] * k) + ',' + Math.round(c[2] * k) + ')'; }

  function sphere(cx, cy, r, tone, alpha, shadowY) {
    var c = TONES[tone];
    ctx.save(); ctx.globalAlpha = alpha;
    // soft contact shadow on the cream floor
    var sg = ctx.createRadialGradient(cx, shadowY, 0, cx, shadowY, r * 1.05);
    sg.addColorStop(0, 'rgba(120,90,50,.20)'); sg.addColorStop(1, 'rgba(120,90,50,0)');
    ctx.save(); ctx.translate(0, 0); ctx.scale(1, .22); ctx.translate(0, shadowY * (1 / .22 - 1));
    ctx.fillStyle = sg; ctx.beginPath(); ctx.arc(cx, shadowY, r * 1.05, 0, 6.2832); ctx.fill(); ctx.restore();
    // body: key light top-left (warm), falloff to tone, cool fill rim bottom-right
    var g = ctx.createRadialGradient(cx - r * .35, cy - r * .4, r * .05, cx, cy, r);
    g.addColorStop(0, '#ffffff'); g.addColorStop(.35, 'rgb(255,250,240)'); g.addColorStop(.7, rgb(c, .97)); g.addColorStop(1, rgb(c, .80));
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, r, 0, 6.2832); ctx.fill();
    // cool fill from lower right
    var f = ctx.createRadialGradient(cx + r * .55, cy + r * .6, 0, cx + r * .55, cy + r * .6, r * .9);
    f.addColorStop(0, 'rgba(190,210,235,.28)'); f.addColorStop(1, 'rgba(190,210,235,0)');
    ctx.fillStyle = f; ctx.beginPath(); ctx.arc(cx, cy, r, 0, 6.2832); ctx.fill();
    // specular blob
    var s = ctx.createRadialGradient(cx - r * .38, cy - r * .5, 0, cx - r * .38, cy - r * .5, r * .32);
    s.addColorStop(0, 'rgba(255,255,255,.95)'); s.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = s; ctx.beginPath(); ctx.ellipse(cx - r * .38, cy - r * .5, r * .3, r * .2, -.5, 0, 6.2832); ctx.fill();
    ctx.restore();
  }

  var hit = [];
  function frame(now) {
    raf = 0;
    if (!visible) return;
    var t = (now - t0) / 1000, m = Math.min(W, H);
    mx += (tx - mx) * .06; my += (ty - my) * .06;
    var sc = Math.min(1, Math.max(0, window.scrollY / Math.max(1, hero.offsetHeight)));
    ctx.clearRect(0, 0, W, H); hit.length = 0;
    // far satellites first (smaller z = farther = fainter, smaller)
    var all = [];
    sats.forEach(function (s, k) { all.push({ o: s, k: 100 + k, sat: true }); });
    big.forEach(function (b, k) { all.push({ o: b, k: k, sat: false }); });
    all.sort(function (a, b) { return a.o.z - b.o.z; });
    all.forEach(function (e) {
      var o = e.o, z = o.z;
      var bobY = Math.sin(t * (e.sat ? o.sp : .35) + o.ph) * (e.sat ? 14 : 10) * dpr;
      var drift = e.sat ? ((o.x + t * o.sp * .008) % 1.1) : o.x;
      var cx = drift * W + mx * 38 * z * dpr;
      var cy = o.y * H + bobY + my * 28 * z * dpr - sc * 90 * z * dpr;
      var r = o.r * m * (1 + sc * .12 * z);
      var q = squish[e.k], sq = 1;
      if (q) { var dt = (now - q.t0) / 1000; sq = 1 + .08 * Math.exp(-dt * 5) * Math.cos(dt * 18); if (dt > 1.2) delete squish[e.k]; }
      var rr = r * sq;
      // depth of field: far = softer/more transparent
      sphere(cx, cy, rr, o.t, e.sat ? .35 + z * .6 : .96, cy + rr * .98);
      if (!e.sat) hit.push({ k: e.k, x: cx, y: cy, r: rr });
    });
    frames++; cv.dataset.frames = String(frames);
    raf = requestAnimationFrame(frame);
  }
  function kick() { if (!raf && visible) raf = requestAnimationFrame(frame); }

  window.addEventListener('pointermove', function (e) {
    var b = cv.getBoundingClientRect();
    tx = ((e.clientX - b.left) / b.width - .5) * 2; ty = ((e.clientY - b.top) / b.height - .5) * 2;
    var px = (e.clientX - b.left) * dpr, py = (e.clientY - b.top) * dpr;
    for (var i = hit.length - 1; i >= 0; i--) {
      var h = hit[i]; if (Math.hypot(px - h.x, py - h.y) < h.r) { if (!squish[h.k] || performance.now() - squish[h.k].t0 > 900) squish[h.k] = { t0: performance.now() }; break; }
    }
  }, { passive: true });
  window.addEventListener('resize', resize);
  if ('IntersectionObserver' in window) new IntersectionObserver(function (es) { visible = es[0].isIntersecting; if (visible) kick(); }, { threshold: 0 }).observe(hero);
  document.addEventListener('visibilitychange', function () { visible = !document.hidden; if (visible) kick(); });
  cv.addEventListener('webglcontextlost', function () { });
  resize(); cv.dataset.renderer = 'canvas2d'; cv.dataset.motion = 'animated'; kick();
})();
