/* Language state for the dashboard: L(zh, en) picks the current language; static text carries data-en. */
window.LANG = new URLSearchParams(location.search).get("lang") || localStorage.getItem("aml-lang") || "zh";
window.L = (zh, en) => (window.LANG === "en" ? (en ?? zh) : zh);
window.applyStaticLang = () => {
  document.documentElement.lang = window.LANG === "en" ? "en" : "zh-CN";
  document.querySelectorAll("[data-en]").forEach(node => {
    if (node.dataset.zh == null) node.dataset.zh = node.innerHTML;
    node.innerHTML = window.LANG === "en" ? node.dataset.en : node.dataset.zh;
  });
};
