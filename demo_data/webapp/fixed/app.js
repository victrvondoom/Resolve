/* Inventory Portal, fixed: every request succeeds and every response is checked. */
var out = document.getElementById('out');

document.getElementById('login-form').addEventListener('submit', function (ev) {
  ev.preventDefault();
  fetch('/demo-app/fixed/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user: document.getElementById('user').value })
  })
    .then(function (r) { if (!r.ok) { throw new Error('login failed: ' + r.status); } return r.json(); })
    .then(function (data) { out.textContent = 'Signed in as ' + data.user; })
    .catch(function (err) { out.textContent = String(err); });
});

document.getElementById('load-inventory').addEventListener('click', function () {
  fetch('/demo-app/fixed/api/inventory')
    .then(function (r) { if (!r.ok) { throw new Error('inventory failed: ' + r.status); } return r.json(); })
    .then(function (data) { out.textContent = data.items.map(function (i) { return i.sku; }).join('\n'); })
    .catch(function (err) { out.textContent = String(err); });
});
