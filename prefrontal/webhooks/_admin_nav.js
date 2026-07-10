// Reveal the operator-only "Admin" link in the top nav.
//
// The link ships hidden in every shared-nav page; this shows it only for an
// operator. Any signed-in user may call GET /admin/whoami (a non-operator just
// sees is_operator:false), so this leaks nothing — a non-operator never sees the
// link, and the /admin endpoints are operator-gated server-side regardless. Auth
// rides the same token/cookie every other fetch on the page uses, so it works for
// both access-code and Google-session sign-ins. Any failure (signed-out, offline)
// simply leaves the link hidden.
(function () {
  var links = document.querySelectorAll("[data-nav-admin]");
  if (!links.length) return;
  var token = null;
  try { token = localStorage.getItem("prefrontal_token"); } catch (e) {}
  fetch("/admin/whoami", { headers: token ? { "X-Prefrontal-Token": token } : {} })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (d && d.is_operator) {
        links.forEach(function (a) { a.style.display = ""; });
      }
    })
    .catch(function () {});
})();
