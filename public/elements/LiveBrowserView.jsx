import { useEffect, useRef, useState } from "react"

// Two modes:
//  * props.url   - a hosted browser's own live view (Kernel), shown in an iframe;
//  * props.relay - Nami's in-page stream: frames arrive over a WebSocket and clicks/keys go back the same way.
// The relay link only works for the signed-in owner (the server also checks the session cookie) and dies with the task.
const SPECIAL = ["Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown",
  "Home", "End", "PageUp", "PageDown"]

function Relay() {
  const img = useRef(null)
  const ws = useRef(null)
  const size = useRef({ w: 1280, h: 800 })
  const [state, setState] = useState("connecting")
  const [host, setHost] = useState("")
  const [focused, setFocused] = useState(false)

  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
    const sock = new WebSocket(`${proto}//${window.location.host}${props.relay}`)
    ws.current = sock
    sock.onopen = () => setState("live")
    sock.onclose = () => setState("closed")
    sock.onmessage = (e) => {
      const m = JSON.parse(e.data)
      if (m.t !== "frame") return
      if (m.w && m.h) size.current = { w: m.w, h: m.h }
      if (img.current) img.current.src = "data:image/jpeg;base64," + m.data
      setHost(m.url || "")
    }
    return () => sock.close()
  }, [])

  const send = (m) => { if (ws.current && ws.current.readyState === 1) ws.current.send(JSON.stringify(m)) }
  const at = (e) => {
    const r = img.current.getBoundingClientRect()
    return { x: (e.clientX - r.left) * size.current.w / r.width, y: (e.clientY - r.top) * size.current.h / r.height }
  }
  const lastMove = useRef(0)
  const onMove = (e) => { const now = Date.now(); if (now - lastMove.current > 50) { lastMove.current = now; send({ t: "move", ...at(e) }) } }
  const onKey = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v") return          // handled by onPaste
    if (SPECIAL.includes(e.key)) { e.preventDefault(); send({ t: "key", key: e.key }); return }
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) { e.preventDefault(); send({ t: "text", text: e.key }) }
  }

  return (
    <div>
      <div className="flex items-center gap-2 px-3 py-1 text-xs bg-muted">
        <span className={state === "live" ? "text-green-600" : "text-amber-600"}>{state === "live" ? "● live" : state}</span>
        <span className="truncate">{host}</span>
        <button className="ml-auto underline" onClick={() => send({ t: "back" })}>Back</button>
      </div>
      <div tabIndex={0} onKeyDown={onKey} onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
        onPaste={(e) => { e.preventDefault(); send({ t: "text", text: e.clipboardData.getData("text") }) }}
        className={"outline-none " + (focused ? "ring-2 ring-green-500" : "")}>
        <img ref={img} alt="Sign-in browser" draggable={false} className="w-full select-none" style={{ cursor: "pointer", minHeight: 200 }}
          onMouseDown={(e) => { e.currentTarget.parentElement.focus(); send({ t: "down", b: e.button, ...at(e) }) }}
          onMouseUp={(e) => send({ t: "up", b: e.button, ...at(e) })}
          onMouseMove={onMove}
          onWheel={(e) => send({ t: "wheel", dx: e.deltaX, dy: e.deltaY, ...at(e) })}
          onContextMenu={(e) => e.preventDefault()} />
      </div>
      {!focused && state === "live" && <div className="px-3 py-1 text-xs text-muted-foreground">Click inside the page to type.</div>}
      {state === "closed" && <div className="px-3 py-1 text-xs text-muted-foreground">This view has ended.</div>}
    </div>
  )
}

export default function LiveBrowserView() {
  return (
    <div className="w-full rounded-xl border-2 border-green-500 overflow-hidden">
      <div className="bg-green-500/10 px-3 py-2 text-sm font-medium">
        {props.interactive ? "You're in control. I'm not watching." : "Live view (read only)"}
      </div>
      {props.relay ? <Relay /> :
        <iframe src={props.url} title="Live browser" className="w-full" style={{ height: 480, border: 0 }} allow="clipboard-read; clipboard-write" />}
    </div>
  )
}
