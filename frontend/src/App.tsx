import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, ApiError, setUnauthorizedHandler } from "./services/api";
import type { AppConfig, User } from "./types/api";
import { ErrorBoundary, ErrorBox, Spinner, ToastProvider } from "./components/common";
import { AppShell } from "./components/AppShell";
import { UploadQueueProvider } from "./components/UploadQueue";
import { RigDataProvider, useRig } from "./hooks/useRigData";
import { useTheme } from "./lib/theme";
import { OverviewPage } from "./pages/OverviewPage";
import { Login } from "./pages/Login";

// Pages that are opened less often load on demand, keeping the first load small.
const SystemPage = lazy(() => import("./pages/SystemPage").then((m) => ({ default: m.SystemPage })));
const ClockPage = lazy(() => import("./pages/ClockPage").then((m) => ({ default: m.ClockPage })));
const StreamPage = lazy(() => import("./pages/StreamPage").then((m) => ({ default: m.StreamPage })));
const UsersPage = lazy(() => import("./pages/UsersPage").then((m) => ({ default: m.UsersPage })));
const StreamsPage = lazy(() => import("./pages/StreamsPage").then((m) => ({ default: m.StreamsPage })));
const LibraryPage = lazy(() => import("./pages/LibraryPage").then((m) => ({ default: m.LibraryPage })));
const ClipperPage = lazy(() => import("./pages/ClipperPage").then((m) => ({ default: m.ClipperPage })));
const AnalyticsPage = lazy(() => import("./pages/AnalyticsPage").then((m) => ({ default: m.AnalyticsPage })));

export default function App() {
  useTheme(); // applies the saved light/dark preference
  const [user, setUser] = useState<User | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [state, setState] = useState<"loading" | "login" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  const boot = useCallback(async () => {
    try {
      const u = await api.me();
      const c = await api.config();
      setUser(u);
      setConfig(c);
      setState("ready");
    } catch (e) {
      const err = e as ApiError;
      if (err.status === 401) setState("login");
      else {
        setError(err.message);
        setState("error");
      }
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => setState("login"));
    boot();
  }, [boot]);

  const logout = async () => {
    await api.logout().catch(() => {});
    setState("login");
  };

  return (
    <ToastProvider>
      {state === "loading" && <div className="flex min-h-screen items-center justify-center"><Spinner /></div>}
      {state === "error" && (
        <div className="mx-auto max-w-lg p-8">
          <ErrorBox>{error}</ErrorBox>
          <button className="btn-secondary mt-3" onClick={() => { setState("loading"); boot(); }}>Retry</button>
        </div>
      )}
      {state === "login" && <Login onLogin={() => { setState("loading"); boot(); }} />}
      {state === "ready" && user && config && (
        <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <RigDataProvider user={user} config={config}>
            <Uploads>
            <AppShell onLogout={logout}>
              <Suspense fallback={<div className="p-6"><Spinner /></div>}>
              <RouteBoundary>
              <Routes>
                <Route path="/" element={<OverviewPage />} />
                <Route path="/streams" element={<StreamsPage />} />
                <Route path="/streams/:id" element={<StreamPage />} />
                <Route path="/library" element={<LibraryPage />} />
                <Route path="/analytics/:id" element={<AnalyticsPage />} />
                <Route path="/system" element={<SystemPage />} />
                <Route path="/clock" element={<ClockPage />} />
                <Route path="/clip/:id" element={<ClipperPage />} />
                {/* old addresses keep working (bookmarks, links in alerts) */}
                <Route path="/health" element={<Navigate to="/system" replace />} />
                <Route path="/events" element={<Navigate to="/system?tab=events" replace />} />
                <Route path="/users" element={<UsersPage />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
              </RouteBoundary>
              </Suspense>
            </AppShell>
            </Uploads>
          </RigDataProvider>
        </BrowserRouter>
      )}
    </ToastProvider>
  );
}

/** Upload queue that refreshes the library when a file lands on the server. */
function Uploads({ children }: { children: React.ReactNode }) {
  const { refreshVideos } = useRig();
  return <UploadQueueProvider onUploaded={refreshVideos}>{children}</UploadQueueProvider>;
}

/** A page that crashes shows a small error box; the menu and top bar keep working, and moving to another page clears it. */
function RouteBoundary({ children }: { children: React.ReactNode }) {
  const { pathname } = useLocation();
  return <ErrorBoundary label="this page" resetKey={pathname}>{children}</ErrorBoundary>;
}
