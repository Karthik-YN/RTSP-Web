import { useState } from "react";
import { api, ApiError } from "../services/api";
import type { User } from "../types/api";
import { ErrorBox, Spinner } from "../components/common";
import { CameraIcon } from "../components/icons";

export function Login({ onLogin }: { onLogin: (u: User) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await api.login(username, password));
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-950 via-blue-950 to-indigo-950 p-4">
      <form onSubmit={submit} className="card w-full max-w-sm space-y-4 p-7 shadow-pop">
        <div className="flex items-center gap-3">
          <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-br from-blue-500 to-indigo-600 text-white"><CameraIcon /></span>
          <div>
            <h1 className="text-lg font-bold">RTSP Studio</h1>
            <p className="text-sm text-muted">Sign in with your test-rig account</p>
          </div>
        </div>
        <div><label className="label" htmlFor="u">User</label>
          <input id="u" className="input" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus /></div>
        <div><label className="label" htmlFor="p">Password</label>
          <input id="p" type="password" className="input" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></div>
        {error && <ErrorBox>{error}</ErrorBox>}
        <button className="btn-primary w-full py-2" disabled={busy || !username || !password}>{busy && <Spinner />} Sign in</button>
      </form>
    </div>
  );
}
