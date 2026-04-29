// chrome.devtools.network.Request entries are HAR-shaped per the spec.
// We carry them through the panel without restructuring.
export interface CapturedRequest {
  id: string;
  entry: chrome.devtools.network.Request;
}
