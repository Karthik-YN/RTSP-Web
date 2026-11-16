import { useState } from "react";
import { usePolling } from "../hooks/usePolling";
import { useRig } from "../hooks/useRigData";
import { api, ApiError } from "../services/api";
import type { User } from "../types/api";
import { Avatar, ConfirmDialog, ErrorBox, Modal, Skeleton, Spinner, useToast } from "../components/common";
import { KeyIcon, PlusIcon, TrashIcon } from "../components/icons";

const ROLE_HELP: Record<User["role"], string> = {
  admin: "Everything, including users and other people's protected streams",
  operator: "Create, start, stop and delete streams; upload videos",
  viewer: "Read-only: see streams and copy URLs",
};

export function UsersPage() {
  const me = useRig().user;
  const toast = useToast();
  const { data, loading, refresh } = usePolling(api.users, 10000);
  const [adding, setAdding] = useState(false);
  const [reset, setReset] = useState<User | null>(null);
  const [removing, setRemoving] = useState<User | null>(null);

  if (!me.is_admin) return <ErrorBox>Only administrators can manage users.</ErrorBox>;

  const setRole = async (u: User, role: User["role"]) => {
    try {
      await api.updateUser(u.username, { role });
      toast("ok", `${u.username} is now ${role}.`);
      refresh();
    } catch (e) {
      toast("bad", (e as ApiError).message);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Users</h1>
          <p className="text-sm text-muted">Accounts that can sign in to this dashboard.</p>
        </div>
        <button className="btn-primary" onClick={() => setAdding(true)}><PlusIcon /> Add user</button>
      </div>
      <div className="card overflow-hidden">
        {loading && !data ? <div className="space-y-2 p-4">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-10" />)}</div> : (
          <div className="overflow-x-auto"><table className="w-full min-w-[32rem]">
            <thead className="bg-surface-2"><tr><th className="th">User</th><th className="th">Role</th><th className="th hidden md:table-cell">Can</th><th className="th" /></tr></thead>
            <tbody className="divide-y divide-line">
              {(data ?? []).map((u) => (
                <tr key={u.username}>
                  <td className="td"><span className="flex items-center gap-2 font-medium"><Avatar name={u.username} size="md" />{u.username}{u.username === me.username && <span className="text-xs text-subtle">(you)</span>}</span></td>
                  <td className="td">
                    <select className="input w-auto py-1" value={u.role} onChange={(e) => setRole(u, e.target.value as User["role"])}
                      aria-label={`Role of ${u.username}`}>
                      <option value="admin">admin</option><option value="operator">operator</option><option value="viewer">viewer</option>
                    </select>
                  </td>
                  <td className="td hidden text-xs text-muted md:table-cell">{ROLE_HELP[u.role]}</td>
                  <td className="td text-right">
                    <div className="flex justify-end gap-1">
                      <button className="btn-ghost btn-xs whitespace-nowrap" onClick={() => setReset(u)}><KeyIcon className="h-3.5 w-3.5" /> Reset password</button>
                      {u.username !== me.username && (
                        <button className="btn-ghost btn-xs text-red-600" onClick={() => setRemoving(u)} aria-label={`Delete ${u.username}`}><TrashIcon className="h-3.5 w-3.5" /></button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </div>
      {adding && <UserDialog onClose={() => setAdding(false)} onSaved={refresh} />}
      {reset && <UserDialog user={reset} onClose={() => setReset(null)} onSaved={refresh} />}
      {removing && (
        <ConfirmDialog title={`Delete ${removing.username}?`} danger confirmLabel="Delete user"
          message={<>They can no longer sign in. Streams they created keep running.</>}
          onCancel={() => setRemoving(null)}
          onConfirm={async () => {
            const u = removing;
            setRemoving(null);
            try { await api.deleteUser(u.username); toast("ok", `${u.username} deleted.`); refresh(); }
            catch (e) { toast("bad", (e as ApiError).message); }
          }} />
      )}
    </div>
  );
}

function UserDialog({ user, onClose, onSaved }: { user?: User; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const [name, setName] = useState(user?.username ?? "");
  const [role, setRole] = useState<User["role"]>(user?.role ?? "operator");
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      if (user) await api.updateUser(user.username, { password: pw });
      else await api.createUser(name.trim(), role, pw);
      toast("ok", user ? `Password for ${user.username} reset.` : `User ${name} created.`);
      onSaved();
      onClose();
    } catch (ex) {
      setErr((ex as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={user ? `Reset password for ${user.username}` : "Add user"} onClose={onClose}>
      <form className="space-y-3" onSubmit={submit}>
        {!user && (
          <>
            <div><label className="label" htmlFor="u-name">User name</label>
              <input id="u-name" className="input" value={name} onChange={(e) => setName(e.target.value)} autoFocus autoComplete="off" /></div>
            <div><label className="label" htmlFor="u-role">Role</label>
              <select id="u-role" className="input" value={role} onChange={(e) => setRole(e.target.value as User["role"])}>
                <option value="operator">operator</option><option value="viewer">viewer</option><option value="admin">admin</option>
              </select>
              <p className="mt-1 text-xs text-muted">{ROLE_HELP[role]}</p></div>
          </>
        )}
        <div><label className="label" htmlFor="u-pw">{user ? "New password" : "Password"} (min. 8 characters)</label>
          <input id="u-pw" type="password" className="input" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" autoFocus={!!user} /></div>
        {err && <ErrorBox>{err}</ErrorBox>}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={busy || pw.length < 8 || (!user && !name.trim())}>{busy && <Spinner />} {user ? "Reset password" : "Create user"}</button>
        </div>
      </form>
    </Modal>
  );
}
