export function Privacy() {
  return (
    <main className="min-h-screen bg-paper text-ink px-4 py-8 sm:px-5 sm:py-10">
      <div className="mx-auto max-w-3xl">
        <header className="mb-6">
          <h1 className="text-xl font-medium tracking-tight">
            api-medic browser extension — privacy
          </h1>
          <p className="mt-1 text-sm text-muted">Last updated: 2026-10-17</p>
        </header>

        <article className="text-sm leading-relaxed space-y-5">
          <p>
            api-medic is an HTTP API troubleshooting tool. The browser
            extension is a DevTools panel that captures network requests
            inside the developer's own DevTools session and lets the developer
            send a selected request to the api-medic analyzer for diagnosis.
          </p>

          <section>
            <h2 className="text-base font-medium mb-2">What data is sent</h2>
            <p>
              Only when you click <strong>Analyze</strong> on a captured
              request, the extension sends that single request to the
              api-medic analyzer. Before it is sent, the extension masks
              credentials on your machine (see the next section). The payload
              contains:
            </p>
            <ul className="list-disc pl-5 mt-2 space-y-1">
              <li>
                Request URL (including query string; credential-like
                parameter values masked)
              </li>
              <li>Request method</li>
              <li>Request headers (credential headers masked)</li>
              <li>
                Request body, if any (secret fields in JSON and form bodies
                masked)
              </li>
              <li>Response status code</li>
              <li>
                Response headers (cookie values, credential headers and
                credential-like URL parameters masked)
              </li>
              <li>
                Response body, if the browser includes it in the capture
                (secret fields in JSON and form bodies masked)
              </li>
              <li>Timing information from the browser</li>
            </ul>
            <p className="mt-2">
              Captured requests are not sent automatically. The extension
              transmits nothing until you select a specific request and click{" "}
              <strong>Analyze</strong>.
            </p>
          </section>

          <section>
            <h2 className="text-base font-medium mb-2">
              What is masked before sending
            </h2>
            <p>
              From extension version 0.2.0, masking happens in the extension
              before anything leaves your browser. A masked value is replaced
              with{" "}
              <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                *
              </code>{" "}
              characters of the same length, so the analyzer can tell that a
              value was there and how long it was, but not what it was.
            </p>
            <ul className="list-disc pl-5 mt-2 space-y-1">
              <li>
                <strong>
                  <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                    Authorization
                  </code>
                </strong>
                : the scheme (Bearer, Basic, …) and any whitespace are kept,
                and the credential is masked. If the credential is a JWT, the
                extension keeps its header and only its expiry (
                <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                  exp
                </code>,{" "}
                <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                  nbf
                </code>) and subject (
                <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                  sub
                </code>) claims, so the analyzer can still tell whether the token
                has expired or is not valid yet. Every other claim is removed
                and the signature is replaced with{" "}
                <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                  REDACTED
                </code>
                . The subject is often a user ID and can be an email address.
              </li>
              <li>
                <strong>
                  <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                    Proxy-Authorization
                  </code>
                </strong>
                : the scheme is kept and the credential is masked.
              </li>
              <li>
                <strong>Cookies</strong>: in Cookie and Set-Cookie headers and
                in the browser's cookie lists, names and attributes (Path,
                Expires, SameSite, …) are kept and values are masked.
              </li>
              <li>
                <strong>Headers whose names mark a secret</strong>, such as
                X-Api-Key, X-Auth-Token, X-CSRF-Token, Private-Token and
                Ocp-Apim-Subscription-Key, and any header whose name ends in
                key or contains token, secret, password, session, signature or
                credential.
              </li>
              <li>
                <strong>URL parameters with credential-like names</strong>,
                such as token, access_token, api_key, sig, signature,
                password, client_secret, client_assertion and code_verifier,
                and any name that ends in key or code, in the query string or
                fragment of the request URL, of the Referer, Location and
                Content-Location headers, and of HTTP/2's :path; and a
                password in the URL itself (
                <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                  https://user:password@host
                </code>
                ).
              </li>
              <li>
                <strong>Bodies</strong>: string values under secret-named keys
                in JSON bodies, and secret-named fields in URL-encoded form
                bodies.
              </li>
              <li>
                <strong>Browser-specific capture fields</strong> whose names
                start with{" "}
                <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                  _
                </code>
                , such as the stack trace of the code that made the request
                and WebSocket messages, are removed.
              </li>
            </ul>
            <p className="mt-2">
              Masking works by name. A secret under a name the extension does
              not recognize, inside a URL path, in a body that is not valid
              JSON or a URL-encoded form, or stored as a number rather than a
              string is sent as captured.
            </p>
          </section>

          <section>
            <h2 className="text-base font-medium mb-2">Where it goes</h2>
            <p>
              The extension makes one network call: an HTTPS POST to{" "}
              <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                https://api-medic.markandrewmarquez.com/api/analyze
              </code>
              . No other host is contacted. The endpoint is implemented as an
              AWS Lambda function behind API Gateway, operated by the project
              author.
            </p>
          </section>

          <section>
            <h2 className="text-base font-medium mb-2">Retention</h2>
            <p>The Lambda function is stateless. Specifically:</p>
            <ul className="list-disc pl-5 mt-2 space-y-1">
              <li>
                No request body or response body is written to a database.
              </li>
              <li>
                No request body or response body is written to S3 or any other
                storage.
              </li>
              <li>
                No request body or response body is written to application
                logs.
              </li>
              <li>
                The function returns a diagnostic report and forgets the
                request.
              </li>
            </ul>
            <p className="mt-2">
              AWS infrastructure-level metrics (request counts, durations,
              error rates) are retained by AWS as part of normal operation and
              contain no payload data.
            </p>
          </section>

          <section>
            <h2 className="text-base font-medium mb-2">Who has access</h2>
            <p>
              Only the project author. The endpoint requires no account and
              stores no per-user records, so there is no identity associated
              with submissions.
            </p>
          </section>

          <section>
            <h2 className="text-base font-medium mb-2">
              What you should know about your captured data
            </h2>
            <p>
              Captured requests can include sensitive material —{" "}
              <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                Authorization
              </code>{" "}
              headers, session cookies, API keys, personally identifying
              information in bodies — depending on what API you are debugging.
              The extension masks the credentials it recognizes (see above),
              but it does not mask personal information in URLs or bodies,
              and it cannot recognize every secret. Treat clicking{" "}
              <strong>Analyze</strong> the same way you would
              treat pasting a request into any third-party debugging tool. If
              a request contains data you do not want to send off your
              machine, do not click <strong>Analyze</strong> on it.
            </p>
          </section>

          <section>
            <h2 className="text-base font-medium mb-2">
              How to use api-medic without sending data to the hosted server
            </h2>
            <p>
              The analyzer that powers this extension is open source. To run
              everything locally:
            </p>
            <ul className="list-disc pl-5 mt-2 space-y-1">
              <li>
                <strong>Web UI</strong>: clone{" "}
                <a
                  className="text-blue-700 underline"
                  href="https://github.com/marky224/api-medic"
                >
                  https://github.com/marky224/api-medic
                </a>{" "}
                and follow the README to run the frontend and backend on
                localhost.
              </li>
              <li>
                <strong>CLI</strong>: the same repository ships an{" "}
                <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                  api-medic
                </code>{" "}
                command-line tool that runs the analysis locally.
              </li>
            </ul>
            <p className="mt-2">
              The hosted demo is a convenience for the browser extension; it
              is not required to use the project.
            </p>
          </section>

          <section>
            <h2 className="text-base font-medium mb-2">Permissions</h2>
            <p>
              The extension declares one host permission:{" "}
              <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                https://api-medic.markandrewmarquez.com/*
              </code>
              . This is required only to allow the privileged DevTools panel
              to POST captured requests to the analyzer without a CORS
              preflight rejection. The extension does not request{" "}
              <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                tabs
              </code>
              ,{" "}
              <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                cookies
              </code>
              ,{" "}
              <code className="font-mono text-xs bg-panel px-1 py-0.5 rounded">
                storage
              </code>
              , or any host permissions for sites you visit. It does not run
              content scripts or a background service worker.
            </p>
          </section>

          <section>
            <h2 className="text-base font-medium mb-2">Contact</h2>
            <p>
              Mark Marquez —{" "}
              <a
                className="text-blue-700 underline"
                href="mailto:me@markandrewmarquez.com"
              >
                me@markandrewmarquez.com
              </a>
            </p>
            <p className="mt-2">
              For issues, including takedown requests for the hosted endpoint,
              open an issue at{" "}
              <a
                className="text-blue-700 underline"
                href="https://github.com/marky224/api-medic/issues"
              >
                https://github.com/marky224/api-medic/issues
              </a>{" "}
              or email the address above.
            </p>
          </section>
        </article>

        <footer className="mt-8 text-sm">
          <a className="text-blue-700 underline" href="/">
            ← Back to api-medic
          </a>
        </footer>
      </div>
    </main>
  );
}
