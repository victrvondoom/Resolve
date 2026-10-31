/* Cinematic intro controller: builds the blocks and the wordmark, plays once per visit (sessionStorage), can be skipped,
   replayed, closed with Escape, and never blocks the link into the app. Reduced-motion users never see it. */
'use strict';
(function () {
  const root = document.getElementById('intro');
  if (!root) return;
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  const word = root.querySelector('.intro-word');
  if (word && !word.childElementCount) {
    [...'RESOLVE'].forEach((ch, i) => { const s = document.createElement('span'); s.textContent = ch; s.style.setProperty('--i', i); word.append(s); });
  }
  // Partner logos: if the official file is not in web/static/brand/, show the partner's plain name instead of a stand-in mark.
  root.querySelectorAll('img[data-fallback]').forEach((img) => {
    const useText = () => {
      const span = document.createElement('span');
      span.className = 'fallback';
      span.textContent = img.dataset.fallback;
      span.setAttribute('data-asset-missing', 'true');
      img.replaceWith(span);
      const cap = img.closest('figure') && img.closest('figure').querySelector('figcaption');
      if (cap) cap.hidden = true;   // the name is already shown; avoid repeating it
    };
    img.addEventListener('error', useText);
    if (img.complete && img.naturalWidth === 0) useText();
  });
  const faces = '<i class="f f-front"></i><i class="f f-back"></i><i class="f f-right"></i><i class="f f-left"></i><i class="f f-top"></i><i class="f f-bottom"></i>';
  root.querySelectorAll('.intro-block').forEach((b) => { b.innerHTML = faces; });

  let timer = null, lastFocus = null;
  function finish() {
    if (timer) { clearTimeout(timer); timer = null; }
    root.classList.add('is-done');
    root.setAttribute('aria-hidden', 'true');
    try { sessionStorage.setItem('resolve_intro_seen', '1'); } catch (_) { /* storage may be blocked */ }
    if (lastFocus && lastFocus.focus) lastFocus.focus({ preventScroll: true });
  }
  function play() {
    lastFocus = document.activeElement;
    root.classList.remove('is-done');
    root.removeAttribute('aria-hidden');
    root.querySelectorAll('.intro-block, .intro-grid, .intro-sweep, .scene, .intro-word span').forEach((n) => {
      n.style.animation = 'none'; void n.offsetWidth; n.style.animation = '';
    });
    root.querySelector('.intro-skip').focus({ preventScroll: true });
    timer = setTimeout(finish, 8400);
  }
  root.querySelector('.intro-skip').addEventListener('click', finish);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !root.classList.contains('is-done')) finish(); });
  document.querySelectorAll('[data-replay-intro]').forEach((b) => b.addEventListener('click', play));

  let seen = false;
  try { seen = sessionStorage.getItem('resolve_intro_seen') === '1'; } catch (_) { /* ignore */ }
  if (reduce.matches || seen) finish(); else play();
})();
