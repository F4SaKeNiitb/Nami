export default function ApprovalCard() {
  return (
    <div className="w-full max-w-md rounded-xl border-2 border-amber-500 bg-card p-4 text-sm space-y-1">
      <div className="font-semibold">✋ Needs your approval</div>
      <div>{props.title}</div>
      {props.total && <div className="text-lg font-semibold">{props.total}</div>}
      {props.pay_with && <div className="text-muted-foreground">Pay with {props.pay_with}</div>}
      {props.summary && <div className="text-muted-foreground whitespace-pre-wrap">{props.summary}</div>}
      <div className="text-xs text-amber-500">{props.reason}</div>
      {props.warning && <div className="text-xs text-red-500">⚠️ {props.warning}</div>}
    </div>
  )
}
