- **Domain hours: tune the domains your todos actually use** ✅ — the "Domain
  hours" editor now surfaces, beyond the canonical five (`shop`/`work`/`home`/
  `kids`/`personal`), any **free-text** domain currently in use on an open todo
  (e.g. a todo tagged `gardening`), so it can be given its own window too. The
  `GET /schedule/domain-windows` payload carries an `order` list (canonical first,
  then in-use extras alphabetically) that the web + iOS editors render from; a
  domain drops off once no open todo uses it. `POST` accepts a domain that is
  canonical *or* in use on a todo (others still 422), so the API won't mint windows
  for domains that don't exist.
