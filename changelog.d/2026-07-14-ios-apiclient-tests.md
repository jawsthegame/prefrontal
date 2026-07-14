- **iOS `APIClient` unit tests** ✅ (#602 follow-up) — adds the test seam flagged
  when the iOS test target landed. `APIClient` gains a direct
  `init(baseURL:token:)` (bypassing the App Group / Keychain the other inits read)
  and its `request(...)` builder is now internal, so `PrefrontalTests` can assert
  the request contract hermetically: URL + query joining, the
  `X-Prefrontal-Token`/`Accept` headers, HTTP method, and the JSON body +
  `Content-Type` on writes. A small `URLProtocol` stub on `URLSession.shared` also
  covers the response path (2xx JSON decode) and error mapping (non-2xx →
  `APIError.http`), no live server needed. Runs in the existing `xcodebuild test`
  CI job.
