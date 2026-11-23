# api-medic — diagnostic report

`GET https://api.example.com/v1` → `200 OK`

| Latency | Body | Protocol | Findings |
|---------|------|----------|----------|
| 10 ms | 0 B | HTTP/1.1 | 1 critical |

## Timing

| Phase | Duration |
|-------|---------:|
| TTFB | 10 ms |
| **Total** | **10 ms** |

## Findings

### [CRITICAL] CORS response missing Access-Control-Allow-Origin
**`http.cors.misconfigured`**

The request was made from origin https://o.example\n\n# Pwned heading\n\n\[critical\]: https://evil.example/tag\r\n> quote, but the response has no Access-Control-Allow-Origin header. The browser will block the response from reaching JS.

**Evidence:**
- `request_origin`: `https://o.example\n\n# Pwned heading\n\n[critical]: https://evil.example/tag\r\n> quote`
- `allow_origin`: `null`

**Suggested fix:** Set Access-Control-Allow-Origin: https://o.example\n\n# Pwned heading\n\n\[critical\]: https://evil.example/tag\r\n> quote on the server (or a wildcard if the API is fully public).
