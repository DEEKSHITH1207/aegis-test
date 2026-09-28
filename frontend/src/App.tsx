import { NavLink, Link, Route, Routes } from "react-router-dom";

import DashboardPage from "./pages/DashboardPage";
import RunPage from "./pages/RunPage";
import SpecificationPage from "./pages/SpecificationPage";
import UploadPage from "./pages/UploadPage";

export default function App() {
  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <Link to="/" className="brand">
            <img src="/favicon.svg" alt="" width={24} height={24} />
            AegisTest
          </Link>
          <nav className="nav" aria-label="Main">
            <NavLink to="/" end>
              Dashboard
            </NavLink>
            <NavLink to="/upload">Upload spec</NavLink>
          </nav>
        </div>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/upload" element={<UploadPage />} />
          <Route path="/specifications/:id" element={<SpecificationPage />} />
          <Route path="/runs/:id" element={<RunPage />} />
          <Route
            path="*"
            element={
              <div className="empty">
                Page not found. <Link to="/">Back to the dashboard</Link>
              </div>
            }
          />
        </Routes>
      </main>
    </>
  );
}
