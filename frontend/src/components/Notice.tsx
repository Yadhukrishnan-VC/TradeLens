export type NoticeState = { tone: "ok" | "bad"; text: string } | null;

export function Notice({ notice, onClose }: { notice: NoticeState; onClose?: () => void }) {
  if (!notice) return null;
  return (
    <div className={`notice ${notice.tone}`} role={notice.tone === "bad" ? "alert" : "status"}>
      <span>{notice.text}</span>
      {onClose && <button className="link" onClick={onClose}>Dismiss</button>}
    </div>
  );
}
