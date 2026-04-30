import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { Privacy } from "./Privacy";
import "./index.css";

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("#root element missing from index.html");

const path = window.location.pathname.replace(/\/+$/, "");
const isPrivacy = path === "/privacy";

createRoot(rootEl).render(
  <StrictMode>{isPrivacy ? <Privacy /> : <App />}</StrictMode>,
);
