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

The request was made from origin https://o.example \[Re-authenticate here\](https://evil.example/login) !\[t\](https://evil.example/p.png) &lt;img src="https://evil.example/p.png"> &lt;https://evil.example/login> \\\[x\\\](https://evil.example/) &amp;lt;b&amp;gt;, but the response has no Access-Control-Allow-Origin header. The browser will block the response from reaching JS.

**Evidence:**
- `request_origin`: `https://o.example [Re-authenticate here](https://evil.example/login) ![t](https://evil.example/p.png) <img src="https://evil.example/p.png"> <https://evil.example/login> \[x\](https://evil.example/) &lt;b&gt;`
- `allow_origin`: `null`

**Suggested fix:** Set Access-Control-Allow-Origin: https://o.example \[Re-authenticate here\](https://evil.example/login) !\[t\](https://evil.example/p.png) &lt;img src="https://evil.example/p.png"> &lt;https://evil.example/login> \\\[x\\\](https://evil.example/) &amp;lt;b&amp;gt; on the server (or a wildcard if the API is fully public).
