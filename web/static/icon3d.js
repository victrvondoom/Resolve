/* RESOLVE 3D model: a CSS-3D device rendered in the browser (no WebGL, no external script).
   Every dimension label is generated from the numbers passed to mount(), so the drawing and the text cannot disagree.
   Drag to rotate; keyboard arrows rotate too; reduced-motion users get no idle sway. */
'use strict';
(function () {
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');

  /* A face is a flat plate centred on the box: it is positioned by its own size, then turned and pushed out by half the box. */
  function face(cls, w, h, transform) {
    const f = document.createElement('div');
    f.className = 'i3d-face ' + cls;
    f.style.width = w + 'px'; f.style.height = h + 'px';
    f.style.marginLeft = (-w / 2) + 'px'; f.style.marginTop = (-h / 2) + 'px';
    f.style.transform = transform;
    return f;
  }

  function mount(host, opts) {
    const W = opts.width, D = opts.depth, H = opts.height;           // mm: width, depth, height of the modelled device
    host.classList.add('i3d-host');
    host.setAttribute('role', 'img');
    host.setAttribute('aria-label', `3D model of a computer: ${W} mm wide, ${D} mm deep, ${H} mm tall. Drag to rotate.`);
    host.tabIndex = 0;
    const s = 0.9;                                                   // drawing scale: 1 mm = 0.9 px (labels show the modelled size)
    const w = W * s, d = D * s, h = H * s;
    const stage = document.createElement('div'); stage.className = 'i3d-stage';
    const box = document.createElement('div'); box.className = 'i3d-box';
    box.append(
      face('i3d-screen', w, h, `translateZ(${d / 2}px)`),                       // front: the display
      face('i3d-back', w, h, `rotateY(180deg) translateZ(${d / 2}px)`),         // back of the lid
      face('i3d-side', d, h, `rotateY(90deg) translateZ(${w / 2}px)`),          // right side
      face('i3d-side', d, h, `rotateY(-90deg) translateZ(${w / 2}px)`),         // left side
      face('i3d-base', w, d, `rotateX(90deg) translateZ(${h / 2}px)`),          // top deck (keyboard)
      face('i3d-base i3d-bottom', w, d, `rotateX(-90deg) translateZ(${h / 2}px)`) // underside
    );
    stage.append(box);
    const dims = document.createElement('div'); dims.className = 'i3d-dims'; dims.setAttribute('aria-hidden', 'true');
    dims.innerHTML = [['i3d-dim-w', `${W} mm wide`], ['i3d-dim-d', `${D} mm deep`], ['i3d-dim-h', `${H} mm tall`]]
      .map(([c, text]) => `<span class="i3d-dim ${c}"><b>${text}</b></span>`).join('');
    const legend = document.createElement('p'); legend.className = 'i3d-legend muted small';
    legend.textContent = `Scale model of the device (${W} × ${D} × ${H} mm). Drag, or use the arrow keys, to rotate.`;
    host.replaceChildren(stage, dims, legend);

    let rx = -16, ry = -30, dragging = false, lx = 0, ly = 0;
    const apply = () => { box.style.transform = `rotateX(${rx}deg) rotateY(${ry}deg)`; };
    apply();
    host.addEventListener('pointerdown', (e) => { dragging = true; lx = e.clientX; ly = e.clientY; host.setPointerCapture(e.pointerId); host.classList.add('dragging'); });
    host.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      ry += (e.clientX - lx) * 0.4; rx = Math.max(-70, Math.min(70, rx - (e.clientY - ly) * 0.4));
      lx = e.clientX; ly = e.clientY; apply();
    });
    const stop = () => { dragging = false; host.classList.remove('dragging'); };
    host.addEventListener('pointerup', stop); host.addEventListener('pointercancel', stop);
    host.addEventListener('keydown', (e) => {
      const step = 8;
      if (e.key === 'ArrowLeft') ry -= step; else if (e.key === 'ArrowRight') ry += step;
      else if (e.key === 'ArrowUp') rx -= step; else if (e.key === 'ArrowDown') rx += step; else return;
      e.preventDefault(); apply();
    });
    if (!(reduce && reduce.matches)) {
      const t0 = performance.now();
      const sway = (now) => { if (!dragging && host.isConnected) { ry = -30 + Math.sin((now - t0) / 1800) * 10; apply(); } requestAnimationFrame(sway); };
      requestAnimationFrame(sway);
    }
  }

  window.Icon3D = {
    mount,
    /* the default model: a 340 × 240 × 230 mm device (illustrative dimensions, stated on the drawing) */
    mountDefault(host) { mount(host, { width: 340, depth: 240, height: 230 }); },
  };
  document.querySelectorAll('[data-icon3d]').forEach((el) => window.Icon3D.mountDefault(el));
})();
