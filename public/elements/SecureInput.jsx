import { useState } from "react"

// Secrets go ONLY to the vault API over same-origin fetch. Never callAction (its payload is saved in chat history).
const KINDS = {
  password: { label: "Website login", fields: [["username", "Username or email", "text"], ["password", "Password (leave empty if the site only sends a one-time code)", "password"]], site: true },
  api_key: { label: "API key", fields: [["key", "API key", "password"]], site: true, sitePlaceholder: "Service it is for (e.g. openrouter.ai)" },
  totp: { label: "Authenticator (2FA) key", fields: [["seed", "Setup key or otpauth:// link", "password"]], site: true },
  card: { label: "Card (demo vault, not PCI)", fields: [["number", "Card number", "password"], ["exp", "Expiry (MM/YY)", "text"], ["cvv", "CVV", "password"]], site: true },
}

async function api(path, method, body) {
  const r = await fetch(`/chainlit/vault${path}`, {
    method, credentials: "include",
    headers: { "Content-Type": "application/json", "X-Requested-With": "nami" },
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(data.detail || `Error ${r.status}`)
  return data
}

// Read an authenticator QR image in the browser (BarcodeDetector). The image and the link inside it never leave this
// page except through the same POST /credentials as a typed key. Resolves to the otpauth:// text or throws a short reason.
async function readQr(file) {
  if (typeof BarcodeDetector === "undefined") throw new Error("This browser can't read QR images. Paste the setup key instead.")
  const bmp = await createImageBitmap(file)
  try {
    const found = await new BarcodeDetector({ formats: ["qr_code"] }).detect(bmp)
    const text = (found.find((c) => (c.rawValue || "").startsWith("otpauth://")) || {}).rawValue
    if (!text) throw new Error("No authenticator QR code found in that image. Paste the setup key instead.")
    return text
  } finally { bmp.close && bmp.close() }
}

export default function SecureInput() {
  // Optional props from a chat question (labels only, never secrets): which site, which kinds to offer.
  const asked = (typeof props !== "undefined" && props) || {}
  const offered = Array.isArray(asked.kinds) && asked.kinds.length ? asked.kinds.filter((x) => KINDS[x]) : null
  const options = offered ? offered.filter((x) => !(x === "totp" && offered.includes("password"))) : Object.keys(KINDS)
  const locked = !!(asked.lock_site && asked.site)
  const [kind, setKind] = useState(options[0] || "password")
  const [label, setLabel] = useState("")
  const [site, setSite] = useState(asked.site || "")
  const [twofa, setTwofa] = useState("")
  const [vals, setVals] = useState({})
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState(null)
  const k = KINDS[kind]

  const reset = () => { setVals({}); setLabel(""); setTwofa(""); if (!locked) setSite(""); setPreview(null) }

  const save = async () => {
    setBusy(true); setMsg(null)
    try {
      const given = Object.fromEntries(Object.entries(vals).filter(([, x]) => x !== ""))   // an empty optional field is not sent
      const v = await api("/credentials", "POST", {
        kind, label: label || k.label, origins: site ? [site] : [], secret: given,
      })
      if (kind === "password" && twofa.trim()) {      // optional authenticator key, saved next to the login and linked to it
        await api("/credentials", "POST", {
          kind: "totp", label: `${label || k.label} 2FA`, origins: site ? [site] : [], secret: { seed: twofa.trim() },
          linked_credential_id: v.id,
        })
      }
      reset()                                         // the secret leaves component state immediately
      setMsg({ ok: true, text: `Saved "${v.label}"${v.meta.last4 ? ` ••••${v.meta.last4}` : ""}. Encrypted, and only usable on ${v.origins.join(", ") || "no site yet (add one so I can use it)"}.` +
        (v.replaced ? " It replaced your older one for this site." : "") + (asked.site ? " Now click \"I've added it\" above and I'll continue." : "") })
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy(false)
  }

  const pickQr = async (e, apply) => {
    const file = e.target.files && e.target.files[0]
    e.target.value = ""                                // forget the file selection
    if (!file) return
    try { apply(await readQr(file)); setMsg({ ok: true, text: "QR read on this device. Review and save." }) }
    catch (err) { setMsg({ ok: false, text: err.message || "Couldn't read that image. Paste the setup key instead." }) }
  }

  const check = async () => {
    try { setPreview(await api("/totp/preview", "POST", { seed: vals.seed || "" })) }
    catch (e) { setPreview({ error: e.message }) }
  }

  return (
    <div className="w-full max-w-md rounded-xl border border-border bg-card p-4 space-y-3 text-sm">
      <div className="font-semibold">🔒 Secure vault</div>
      <div className="text-muted-foreground">This goes straight to the encrypted vault. It never enters the chat or the model.</div>
      {options.length > 1 && (
        <select className="w-full rounded-md border border-input bg-background p-2" value={kind}
          onChange={(e) => { setKind(e.target.value); setVals({}); setTwofa(""); setPreview(null) }}>
          {options.map((id) => <option key={id} value={id}>{KINDS[id].label}</option>)}
        </select>
      )}
      <input className="w-full rounded-md border border-input bg-background p-2" placeholder="Name (e.g. Swiggy login)"
        value={label} onChange={(e) => setLabel(e.target.value)} />
      {k.site && <input className="w-full rounded-md border border-input bg-background p-2" placeholder={k.sitePlaceholder || "Website it is for (e.g. swiggy.com)"}
        value={site} readOnly={locked} onChange={(e) => setSite(e.target.value)} />}
      {k.fields.map(([f, ph, type]) => (
        <input key={f} type={type} autoComplete="off" className="w-full rounded-md border border-input bg-background p-2" placeholder={ph}
          value={vals[f] || ""} onChange={(e) => setVals({ ...vals, [f]: e.target.value })} />
      ))}
      {kind === "password" && (
        <input type="password" autoComplete="off" className="w-full rounded-md border border-input bg-background p-2"
          placeholder="Authenticator (2FA) setup key, optional" value={twofa} onChange={(e) => setTwofa(e.target.value)} />
      )}
      {(kind === "password" || kind === "totp") && (
        <label className="block text-xs text-muted-foreground">Or upload the QR image from the site (read on this device, not uploaded):
          <input type="file" accept="image/*" className="block mt-1"
            onChange={(e) => pickQr(e, (t) => (kind === "totp" ? setVals({ ...vals, seed: t }) : setTwofa(t)))} />
        </label>
      )}
      {kind === "totp" && (
        <div>
          <button className="underline" onClick={check}>Show the current code to compare with your app</button>
          {preview && (preview.error ? <div className="text-red-500">{preview.error}</div>
            : <div className="font-mono text-lg">{preview.code} <span className="text-muted-foreground text-xs">({preview.seconds_left}s)</span>
              {!preview.standard && <div className="text-xs text-amber-500">Non-standard settings: I'll generate the code myself for each sign-in.</div>}</div>)}
        </div>
      )}
      {kind === "card" && <div className="text-amber-500 text-xs">Demo vault, not PCI-certified. Prefer a virtual or limited card. I'll still ask before every purchase.</div>}
      {kind === "totp" && <div className="text-amber-500 text-xs">Keeping the password and its 2FA key together makes them a single factor. For high-value accounts, sign in yourself instead.</div>}
      <button disabled={busy} onClick={save} className="w-full rounded-md bg-primary text-primary-foreground p-2 font-medium disabled:opacity-50">
        {busy ? "Saving…" : "Save securely"}
      </button>
      {msg && <div className={msg.ok ? "text-green-500" : "text-red-500"}>{msg.text}</div>}
    </div>
  )
}
