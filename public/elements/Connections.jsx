import { useEffect, useState } from "react"

async function api(path, method = "GET", body = null, base = "/chainlit/vault") {
  const r = await fetch(`${base}${path}`, { method, credentials: "include",
    headers: { "X-Requested-With": "nami", ...(body ? { "Content-Type": "application/json" } : {}) }, body: body ? JSON.stringify(body) : undefined })
  if (!r.ok) throw new Error(`Error ${r.status}`)
  return r.json()
}

export default function Connections() {
  const [creds, setCreds] = useState([])
  const [sessions, setSessions] = useState([])
  const [log, setLog] = useState([])
  const [only, setOnly] = useState(null)       // show the activity of one saved item
  const [skills, setSkills] = useState([])
  const [err, setErr] = useState(null)
  const [note, setNote] = useState(null)

  const load = async (credId = only) => {
    try {
      setCreds(await api("/credentials")); setSessions(await api("/sessions")); setLog(await api(`/activity${credId ? `?credential_id=${credId}` : ""}`)); setSkills(await api("", "GET", null, "/chainlit/skills"))
      setErr(null)
    } catch (e) { setErr(e.message) }
  }
  useEffect(() => { load() }, [])

  const revoke = async (path) => {
    const r = await api(path, "DELETE")
    setNote(r.browser_cleared === false
      ? "Removed, but your browser is busy with a task, so the site may still be signed in. Use Sign out & forget under Signed-in sites once it finishes."
      : r.signed_out && r.signed_out.length ? `Removed, and signed you out of ${r.signed_out.join(", ")}.` : null)
    load()
  }
  const filter = (id) => { setOnly(id); load(id) }

  return (
    <div className="space-y-4 text-sm">
      {err && <div className="text-red-500">{err}</div>}
      {note && <div className="text-amber-500">{note}</div>}
      <section>
        <div className="font-semibold mb-1">Connected accounts</div>
        {(() => {
          const g = creds.find((c) => c.kind === "oauth" && c.meta.provider === "google")
          return g
            ? <div className="flex items-center justify-between border-b border-border py-1">
                <div>Google <span className="text-xs text-muted-foreground">{g.meta.account || "connected"} · Drive, Gmail (read), send after approval</span></div>
                <button className="text-red-500 underline" onClick={() => revoke(`/credentials/${g.id}`)}>Disconnect</button>
              </div>
            : <div className="flex items-center justify-between py-1">
                <div className="text-muted-foreground">Google (Gmail, Drive): not connected</div>
                <a className="underline" href="/chainlit/connect/google/start" target="_blank" rel="noopener">Connect Google</a>
              </div>
        })()}
        <div className="text-xs text-muted-foreground mt-1">For other sites, save a login below, or sign in once inside the chat when I ask. No window opens on the server.</div>
      </section>
      <section>
        <div className="font-semibold mb-1">Saved logins & keys</div>
        {creds.length === 0 && <div className="text-muted-foreground">Nothing saved yet.</div>}
        {creds.filter((c) => c.kind !== "oauth").map((c) => (
          <div key={c.id} className="flex items-center justify-between border-b border-border py-1">
            <div>
              <div>{c.label} <span className="text-xs text-muted-foreground">{c.kind}{c.meta.last4 ? ` ••••${c.meta.last4}` : ""}</span></div>
              <div className="text-xs text-muted-foreground">{c.origins.join(", ") || "server-side only"} · {c.last_used_at ? `used ${new Date(c.last_used_at).toLocaleString()}` : "never used"}</div>
            </div>
            <div className="space-x-2">
              <button className="underline" onClick={() => filter(c.id)}>Activity</button>
              <button className="text-red-500 underline" onClick={() => revoke(`/credentials/${c.id}`)}>Revoke</button>
            </div>
          </div>
        ))}
      </section>
      <section>
        <div className="font-semibold mb-1">Learned workflows</div>
        {skills.length === 0 && <div className="text-muted-foreground">Nothing learned yet. After a verified task I save the steps here.</div>}
        {skills.map((k) => (
          <div key={k.id} className="border-b border-border py-1">
            <div className="flex items-center justify-between">
              <div>{k.intent} <span className="text-xs text-muted-foreground">{k.site} · v{k.version} · {k.status}{k.kind === "snippet" ? " · code" : ""}</span></div>
              <div className="space-x-2">
                {k.status === "candidate" && <button className="underline" onClick={async () => { await api(`/${k.id}/approve`, "POST", { expected_sha256: k.sha256 }, "/chainlit/skills"); load() }}>Approve</button>}
                <button className="text-red-500 underline" onClick={async () => { await api(`/${k.id}`, "DELETE", null, "/chainlit/skills"); load() }}>Revoke</button>
              </div>
            </div>
            <div className="text-xs text-muted-foreground">{k.steps.join(" → ")}</div>
            {k.code && <pre className="text-xs bg-muted p-1 overflow-auto">{k.code}</pre>}
            <div className="text-xs text-muted-foreground">hash {k.sha256.slice(0, 12)} · used {k.success_count}×</div>
          </div>
        ))}
      </section>
      <section>
        <div className="font-semibold mb-1">Signed-in sites</div>
        {sessions.length === 0 && <div className="text-muted-foreground">No saved sessions.</div>}
        {sessions.map((s) => (
          <div key={s} className="flex items-center justify-between border-b border-border py-1">
            <span>{s}</span><button className="text-red-500 underline" onClick={() => revoke(`/sessions/${s}`)}>Sign out & forget</button>
          </div>
        ))}
      </section>
      <section>
        <div className="font-semibold mb-1">Activity {only && <button className="text-xs underline font-normal" onClick={() => filter(null)}>show all</button>}</div>
        {log.length === 0 && <div className="text-muted-foreground">Nothing has used your saved items yet.</div>}
        {log.map((r, i) => (
          <div key={i} className="text-xs text-muted-foreground">
            {new Date(r.ts).toLocaleString()} · <span className={r.ok ? "" : "text-red-500"}>{r.text}</span>
          </div>
        ))}
      </section>
      <button className="underline" onClick={() => load()}>Refresh</button>
    </div>
  )
}
