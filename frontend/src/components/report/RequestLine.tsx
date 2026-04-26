import type { RequestSummary, ResponseSummary } from "../../lib/types";

function statusTone(code: number): string {
  if (code >= 500) return "bg-red-50 text-red-700";
  if (code >= 400) return "bg-red-50 text-red-700";
  if (code >= 300) return "bg-amber-50 text-amber-700";
  if (code >= 200) return "bg-emerald-50 text-emerald-700";
  return "bg-blue-50 text-blue-700";
}

interface RequestLineProps {
  request: RequestSummary;
  response: ResponseSummary | null | undefined;
}

export function RequestLine({ request, response }: RequestLineProps) {
  return (
    <div className="flex items-center justify-between gap-3 mb-5 px-3 py-2.5 bg-sunken rounded-lg">
      <span className="font-mono text-[13px] overflow-hidden text-ellipsis whitespace-nowrap min-w-0">
        <span className="font-medium text-blue-700">{request.method}</span>{" "}
        {request.url}
      </span>
      {response ? (
        <span
          className={`text-xs px-2.5 py-1 rounded-lg font-medium whitespace-nowrap ${statusTone(
            response.status_code,
          )}`}
        >
          {response.status_code} {response.status_text}
        </span>
      ) : (
        <span className="text-xs px-2.5 py-1 rounded-lg font-medium whitespace-nowrap bg-red-50 text-red-700">
          No response
        </span>
      )}
    </div>
  );
}
