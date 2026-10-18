# api-medic browser extension — privacy

Last updated: 2026-10-17

api-medic is an HTTP API troubleshooting tool. The browser extension is a DevTools panel that captures network requests inside the developer's own DevTools session and lets the developer send a selected request to the api-medic analyzer for diagnosis.

## What data is sent

Only when you click **Analyze** on a captured request, the extension sends that single request to the api-medic analyzer. Before it is sent, the extension masks credentials on your machine (see the next section). The payload contains:

- Request URL (including query string; credential-like parameter values masked)
- Request method
- Request headers (credential headers masked)
- Request body, if any (secret fields in JSON and form bodies masked)
- Response status code
- Response headers (cookie values, credential headers and credential-like URL parameters masked)
- Response body, if the browser includes it in the capture (secret fields in JSON and form bodies masked)
- Timing information from the browser

Captured requests are not sent automatically. The extension transmits nothing until you select a specific request and click **Analyze**.

## What is masked before sending

From extension version 0.2.0, masking happens in the extension before anything leaves your browser. A masked value is replaced with `*` characters of the same length, so the analyzer can tell that a value was there and how long it was, but not what it was.

- **`Authorization`**: the scheme (`Bearer`, `Basic`, …) and any whitespace are kept, and the credential is masked. If the credential is a JWT, the extension keeps its header and only its expiry (`exp`, `nbf`) and subject (`sub`) claims, so the analyzer can still tell whether the token has expired or is not valid yet. Every other claim is removed and the signature is replaced with `REDACTED`. The subject is often a user ID and can be an email address.
- **`Proxy-Authorization`**: the scheme is kept and the credential is masked.
- **Cookies**: in `Cookie` and `Set-Cookie` headers and in the browser's cookie lists, names and attributes (`Path`, `Expires`, `SameSite`, …) are kept and values are masked.
- **Headers whose names mark a secret**, such as `X-Api-Key`, `X-Auth-Token`, `X-CSRF-Token`, `Private-Token` and `Ocp-Apim-Subscription-Key`, and any header whose name ends in `key` or contains `token`, `secret`, `password`, `session`, `signature` or `credential`.
- **URL parameters with credential-like names**, such as `token`, `access_token`, `api_key`, `sig`, `signature`, `password`, `client_secret`, `client_assertion` and `code_verifier`, and any name that ends in `key` or `code`, in the query string or fragment of the request URL, of the `Referer`, `Location` and `Content-Location` headers, and of HTTP/2's `:path`; and a password in the URL itself (`https://user:password@host`).
- **Bodies**: string values under secret-named keys in JSON bodies, and secret-named fields in URL-encoded form bodies.
- **Browser-specific capture fields** whose names start with `_`, such as the stack trace of the code that made the request and WebSocket messages, are removed.

Masking works by name. A secret under a name the extension does not recognize, inside a URL path, in a body that is not valid JSON or a URL-encoded form, or stored as a number rather than a string is sent as captured.

## Where it goes

The extension makes one network call: an HTTPS POST to `https://api-medic.markandrewmarquez.com/api/analyze`. No other host is contacted. The endpoint is implemented as an AWS Lambda function behind API Gateway, operated by the project author.

## Retention

The Lambda function is stateless. Specifically:

- No request body or response body is written to a database.
- No request body or response body is written to S3 or any other storage.
- No request body or response body is written to application logs.
- The function returns a diagnostic report and forgets the request.

AWS infrastructure-level metrics (request counts, durations, error rates) are retained by AWS as part of normal operation and contain no payload data.

## Who has access

Only the project author. The endpoint requires no account and stores no per-user records, so there is no identity associated with submissions.

## What you should know about your captured data

Captured requests can include sensitive material — `Authorization` headers, session cookies, API keys, personally identifying information in bodies — depending on what API you are debugging. The extension masks the credentials it recognizes (see above), but it does not mask personal information in URLs or bodies, and it cannot recognize every secret. Treat clicking **Analyze** the same way you would treat pasting a request into any third-party debugging tool. If a request contains data you do not want to send off your machine, do not click **Analyze** on it.

## How to use api-medic without sending data to the hosted server

The analyzer that powers this extension is open source. To run everything locally:

- **Web UI**: clone https://github.com/marky224/api-medic and follow the README to run the frontend and backend on localhost.
- **CLI**: the same repository ships an `api-medic` command-line tool that runs the analysis locally.

The hosted demo is a convenience for the browser extension; it is not required to use the project.

## Permissions

The extension declares one host permission: `https://api-medic.markandrewmarquez.com/*`. This is required only to allow the privileged DevTools panel to POST captured requests to the analyzer without a CORS preflight rejection. The extension does not request `tabs`, `cookies`, `storage`, or any host permissions for sites you visit. It does not run content scripts or a background service worker.

## Contact

Mark Marquez — me@markandrewmarquez.com

For issues, including takedown requests for the hosted endpoint, open an issue at https://github.com/marky224/api-medic/issues or email the address above.
