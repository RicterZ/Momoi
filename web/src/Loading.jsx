export default function Loading({ children = "正在读取 Momoi 的生活记录…" }) {
  return (
    <div className="loading" role="status">
      <span className="loading-mark" aria-hidden="true">M</span>
      <span>{children}</span>
    </div>
  );
}
