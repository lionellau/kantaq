import { NavLink, Outlet } from "react-router-dom";
import { palette } from "../lib/ui";

const NAV = [
  { to: "/", label: "Backlog", end: true },
  { to: "/memory", label: "Memory", end: false },
  { to: "/inbox", label: "Inbox", end: false },
  { to: "/agents", label: "Agents", end: false },
  { to: "/settings", label: "Settings", end: false },
];

export default function Layout() {
  return (
    <div
      style={{
        display: "flex",
        minHeight: "100vh",
        fontFamily: "system-ui, sans-serif",
        color: palette.text,
      }}
    >
      <nav
        aria-label="Primary"
        style={{
          width: 200,
          borderRight: `1px solid ${palette.border}`,
          padding: "1rem",
          flexShrink: 0,
        }}
      >
        <div style={{ fontWeight: 700, fontSize: "1.1rem", marginBottom: "1.25rem" }}>kantaq</div>
        <ul
          style={{
            listStyle: "none",
            padding: 0,
            margin: 0,
            display: "flex",
            flexDirection: "column",
            gap: 2,
          }}
        >
          {NAV.map((item) => (
            <li key={item.to}>
              <NavLink
                to={item.to}
                end={item.end}
                style={({ isActive }) => ({
                  display: "block",
                  padding: "0.4rem 0.6rem",
                  borderRadius: 6,
                  textDecoration: "none",
                  // One gray family from a single source (lib/ui palette): the
                  // active item carries the only emphasis, inactive items recede.
                  color: isActive ? palette.text : palette.muted,
                  background: isActive ? palette.surface : "transparent",
                  fontWeight: isActive ? 600 : 400,
                })}
              >
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <main style={{ flex: 1, padding: "2rem", maxWidth: "60rem" }}>
        <Outlet />
      </main>
    </div>
  );
}
