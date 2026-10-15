export default function ResultCard() {
  const verified = props.verified
  const facts = props.facts || []
  return (
    <div className={`w-full max-w-md rounded-xl border-2 bg-card p-4 text-sm space-y-2 ${verified ? "border-emerald-500" : "border-amber-500"}`}>
      <div className={`font-semibold ${verified ? "text-emerald-500" : "text-amber-500"}`}>
        {verified ? `✓ Done and checked on the page (${Math.round(props.confidence * 100)}%)` : "⚠️ Done, but I couldn't fully verify it"}
      </div>
      <div className="whitespace-pre-wrap">{props.summary}</div>
      {facts.length > 0 && (
        <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1">
          {facts.map(([k, v]) => (<><dt className="text-muted-foreground">{k}</dt><dd className="font-medium">{v}</dd></>))}
        </dl>
      )}
      {!verified && props.unverified?.length > 0 && <div className="text-xs text-amber-500">Not confirmed: {props.unverified.join(", ")}</div>}
      {props.url && <div className="text-xs text-muted-foreground truncate">{props.url}</div>}
    </div>
  )
}
