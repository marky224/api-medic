export function Privacy() {
  return (
    <main className="min-h-screen bg-paper text-ink px-4 py-8 sm:px-5 sm:py-10">
      <div className="mx-auto max-w-3xl">
        <header className="mb-6">
          <h1 className="text-xl font-medium tracking-tight">
            api-medic browser extension — privacy
          </h1>
          <p className="mt-1 text-sm text-muted">Last updated: 2026-04-29</p>
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
              api-medic analyzer. The payload contains:
            </p>
            <ul className="list-disc pl-5 mt-2 space-y-1">
              <li>Request URL (including query string)</li>
              <li>Request method</li>
              <li>Request headers</li>
              <li>Request body (if any)</li>
              <li>Response status code</li>
              <li>Response headers</li>
              <li>Response body (if any)</li>
              <li>Timing information from the browser</li>
            </ul>
            <p className="mt-2">
              Captured requests are not sent automatically. The extension
              transmits nothing until you select a specific request and click{" "}
              <strong>Analyze</strong>.
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
              Treat clicking <strong>Analyze</strong> the same way you would
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
