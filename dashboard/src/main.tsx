import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import "./index.css";
import { Shell } from "./App";
import { Cases } from "./pages/Cases";
import { CaseDetail } from "./pages/CaseDetail";
import { Insights } from "./pages/Insights";
import { Login } from "./pages/Login";
import { Orders } from "./pages/Orders";
import { Settings } from "./pages/Settings";

const router = createBrowserRouter(
  [
    { path: "/login", element: <Login /> },
    {
      path: "/",
      element: <Shell />,
      children: [
        { index: true, element: <Navigate to="/cases" replace /> },
        { path: "cases", element: <Cases /> },
        { path: "cases/:id", element: <CaseDetail /> },
        { path: "orders", element: <Orders /> },
        { path: "insights", element: <Insights /> },
        { path: "settings", element: <Settings /> },
      ],
    },
  ],
  { basename: "/dashboard" },
);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
