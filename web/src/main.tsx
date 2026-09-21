import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles/app.css";

// eslint-disable-next-line no-console
console.log(
  "%c📓 Adaptive Tutor %cbuilt by B Vishnu Priyan",
  "font-weight:700;font-size:14px",
  "color:#888;font-size:12px",
);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
