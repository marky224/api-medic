# api-medic — diagnostic report

`GET https://api.example.com/v1` → `200 OK`

| Latency | Body | Protocol | Findings |
|---------|------|----------|----------|
| 10 ms | 0 B | HTTP/1.1 \| 0 \|\n\| x \| y \| z \| w \| !\[t\](https://evil.example/p.png) \\\| | 0 |

## Timing

| Phase | Duration |
|-------|---------:|
| TTFB | 10 ms |
| **Total** | **10 ms** |

## Findings

_No findings — the request looks healthy._
