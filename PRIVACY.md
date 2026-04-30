# api-medic browser extension — privacy

Last updated: 2026-04-29

api-medic is an HTTP API troubleshooting tool. The browser extension is a DevTools panel that captures network requests inside the developer's own DevTools session and lets the developer send a selected request to the api-medic analyzer for diagnosis.

## What data is sent

Only when you click **Analyze** on a captured request, the extension sends that single request to the api-medic analyzer. The payload contains:

- Request URL (including query string)
- Request method
- Request headers
- Request body (if any)
- Response status code
- Response headers
- Response body (if any)
- Timing information from the browser

Captured requests are not sent automatically. The extension transmits nothing until you select a specific request and click **Analyze**.

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

Captured requests can include sensitive material — `Authorization` headers, session cookies, API keys, personally identifying information in bodies — depending on what API you are debugging. Treat clicking **Analyze** the same way you would treat pasting a request into any third-party debugging tool. If a request contains data you do not want to send off your machine, do not click **Analyze** on it.

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
