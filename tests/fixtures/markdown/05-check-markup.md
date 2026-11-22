# api-medic — diagnostic report

`GET https://api.example.com/v1/users` → `200 OK`

| Latency | Body | Protocol | Findings |
|---------|------|----------|----------|
| 120 ms | 0 B | HTTP/2 | 2 critical |

## Timing

| Phase | Duration |
|-------|---------:|
| **Total** | **120 ms** |

## Findings

### [CRITICAL] Bearer token is not yet valid
**`auth.jwt.not_yet_valid`**

The JWT's `nbf` claim is 2h 0m in the future. The server will reject it until then. Likely a clock-skew issue between the token issuer and the local machine.

**Evidence:**
- `nbf`: `2026-11-22T23:52:01Z`
- `valid_in_seconds`: `7200`
- `sub`: `user_123`

**Suggested fix:** Check the system clocks on the issuer and the client. If skew is small, wait it out; otherwise sync NTP.

### [CRITICAL] Weak TLS protocol negotiated (TLSv1.1)
**`network.tls.weak_protocol`**

The connection negotiated TLSv1.1. Anything below TLS 1.2 is considered insecure and is rejected by modern clients (browsers, Python 3.12+, recent OpenSSL builds).

**Evidence:**
- `negotiated_version`: `TLSv1.1`
- `minimum_safe_version`: `TLSv1.2`

**Suggested fix:** Disable TLS 1.0 and 1.1 on the server. With nginx: `ssl_protocols TLSv1.2 TLSv1.3;`.
