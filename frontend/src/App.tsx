import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { api, ApiError, setUnauthorizedHandler } from "./services/api";
import type { AppConfig, User } from "./types/api";
import { ErrorBox, Spinner, ToastProvider } from "./components/common";
import { AppShell } from "./components/AppShell";
import { UploadQueueProvider } from "./components/UploadQueue";
import { RigDataProvider, useRig } from "./hooks/useRigData";
import { useTheme } from "./lib/theme";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { EventsPage } from "./pages/EventsPage";
import { HealthPage } from "./pages/HealthPage";
import { LibraryPage } from "./pages/LibraryPage";
import { Login } from "./pages/Login";
import { StreamPage } from "./pages/StreamPage";
import { StreamsPage } from "./pages/StreamsPage";
import { UsersPage } from "./pages/UsersPage";

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
        <BrowserRouter>
          <RigDataProvider user={user} config={config}>
            <Uploads>
            <AppShell onLogout={logout}>
              <Routes>
                <Route path="/" element={<Navigate to="/streams" replace />} />
                <Route path="/streams" element={<StreamsPage />} />
                <Route path="/streams/:id" element={<StreamPage />} />
                <Route path="/library" element={<LibraryPage />} />
                <Route path="/analytics/:id" element={<AnalyticsPage />} />
                <Route path="/events" element={<EventsPage />} />
                <Route path="/health" element={<HealthPage />} />
                <Route path="/users" element={<UsersPage />} />
                <Route path="*" element={<Navigate to="/streams" replace />} />
              </Routes>
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
