/* Inventory Portal: deliberately faulty. Every fault below is genuine and observable in a real browser. */

// Fault 1: a telemetry call to a link-local address. The cloud browser's route guard must block it.
fetch('http://169.254.169.254/latest/meta-data/').catch(function () { /* the page ignores the failure */ });

// Fault 2: calls a function that does not exist -> real ReferenceError (pageerror) on load.
setTimeout(function () { initInventoryTable(); }, 0);   // async so the rest of the script still runs

var out = document.getElementById('out');

document.getElementById('login-form').addEventListener('submit', function (ev) {
  ev.preventDefault();
  fetch('/demo-app/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user: document.getElementById('user').value })
  })
    .then(function (r) { return r.json(); })
    // Fault 3: the server answers 500 {"error": ...}; the page never checks r.ok and calls an undefined helper.
    .then(function (data) { renderSession(data); });
});

document.getElementById('load-inventory').addEventListener('click', function () {
  fetch('/demo-app/api/inventory')                       // Fault 4: endpoint does not exist (404)
    .then(function (r) { return r.json(); })
    // Fault 5: reads .items of an error payload -> real TypeError
    .then(function (data) { out.textContent = data.items.map(function (i) { return i.sku; }).join('\n'); });
});
