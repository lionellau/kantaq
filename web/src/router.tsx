import { type RouteObject, createBrowserRouter } from "react-router-dom";
import Layout from "./components/Layout";
import Agents from "./routes/Agents";
import Backlog from "./routes/Backlog";
import Inbox from "./routes/Inbox";
import Memory from "./routes/Memory";
import Settings from "./routes/Settings";
import TicketPage from "./routes/TicketPage";
import Members from "./routes/settings/Members";
import MyAgent from "./routes/settings/MyAgent";

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
      { path: "settings/members", element: <Members /> },
      { path: "settings/my-agent", element: <MyAgent /> },
    ],
  },
];

export const router = createBrowserRouter(routes);
