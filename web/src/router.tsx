import { type RouteObject, createBrowserRouter } from "react-router-dom";
import Layout from "./components/Layout";
import Agents from "./routes/Agents";
import Backlog from "./routes/Backlog";
import Inbox from "./routes/Inbox";
import Memory from "./routes/Memory";
import Settings from "./routes/Settings";
import TicketPage from "./routes/TicketPage";
import Devices from "./routes/settings/Devices";
import Export from "./routes/settings/Export";
import Identity from "./routes/settings/Identity";
import Members from "./routes/settings/Members";
import MyAgent from "./routes/settings/MyAgent";
import Sync from "./routes/settings/Sync";
import Telemetry from "./routes/settings/Telemetry";
import Workspace from "./routes/settings/Workspace";

// The 5-page shell (FR-E18-1) plus the tracker and settings subpages
// (E19/E21). Routes are exported so tests can build a memory router from the
// same config.
export const routes: RouteObject[] = [
  {
    element: <Layout />,
    children: [
      { index: true, element: <Backlog /> },
      { path: "tickets/:ticketId", element: <TicketPage /> },
      { path: "memory", element: <Memory /> },
      { path: "inbox", element: <Inbox /> },
      { path: "agents", element: <Agents /> },
      { path: "settings", element: <Settings /> },
      { path: "settings/workspace", element: <Workspace /> },
      { path: "settings/identity", element: <Identity /> },
      { path: "settings/devices", element: <Devices /> },
      { path: "settings/sync", element: <Sync /> },
      { path: "settings/export", element: <Export /> },
      { path: "settings/members", element: <Members /> },
      { path: "settings/my-agent", element: <MyAgent /> },
      { path: "settings/telemetry", element: <Telemetry /> },
    ],
  },
];

export const router = createBrowserRouter(routes);
