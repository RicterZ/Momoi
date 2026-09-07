export default function Loading({ children = "正在读取 Momoi 的生活记录…" }) {
  return (
    <div className="loading">
      <span className="loading-mark">M</span>
      <span>{children}</span>
    </div>
  );
}
