# api-medic — diagnostic report

```GET https://api.example.com/v1?q=a`![t](https://evil.example/p.png)``x``` → ``200 `OK``

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

The request was made from origin https://o.example`, but the response has no Access-Control-Allow-Origin header. The browser will block the response from reaching JS.

**Evidence:**
- `request_origin`: `` https://o.example` ``
- `allow_origin`: `null`

**Suggested fix:** Set Access-Control-Allow-Origin: https://o.example` on the server (or a wildcard if the API is fully public).
