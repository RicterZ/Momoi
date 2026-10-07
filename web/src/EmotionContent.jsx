import { useState } from "react";

function EmotionImage({ slug }) {
  const [failed, setFailed] = useState(false);
  const marker = `emotion://${slug}`;
  return failed ? <span>{marker}</span> : <img
    className="inline-emotion"
    src={`/api/emotions/${encodeURIComponent(slug)}/asset`}
    alt={marker}
    title={marker}
    loading="lazy"
    onError={() => setFailed(true)}
  />;
}

export default function EmotionContent({ text, className = "message-content" }) {
  const lines = String(text || "").split("\n");
  return <div className={className}>{lines.map((line, index) => {
    const match = /^emotion:\/\/([a-z0-9][a-z0-9._-]{0,63})$/.exec(line.trim());
    return <span key={index}>{index > 0 && <br />}{match
      ? <EmotionImage key={match[1]} slug={match[1]} />
      : line.trim() === "qq://poke" ? "[戳一戳用户]" : line}</span>;
  })}</div>;
}
