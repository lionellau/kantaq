import { type RouteObject, createBrowserRouter } from "react-router-dom";
import Layout from "./components/Layout";
import Agents from "./routes/Agents";
import Backlog from "./routes/Backlog";
import Inbox from "./routes/Inbox";
import Memory from "./routes/Memory";
import Settings from "./routes/Settings";

// The 5-page shell (FR-E18-1). Routes are exported so tests can build a
// memory router from the same config.
export const routes: RouteObject[] = [
  {
    element: <Layout />,
    children: [
      { index: true, element: <Backlog /> },
      { path: "memory", element: <Memory /> },
      { path: "inbox", element: <Inbox /> },
      { path: "agents", element: <Agents /> },
      { path: "settings", element: <Settings /> },
    ],
  },
];

export const router = createBrowserRouter(routes);
