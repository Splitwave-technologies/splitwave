// Тема применяется до отрисовки, чтобы страница не мигала. Первый запуск: тема устройства; дальше — выбор пользователя.
(function () {
  var t = null;
  try { t = localStorage.getItem("dsp_theme"); } catch (e) { /* без хранилища */ }
  if (t !== "light" && t !== "dark") t = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.dataset.theme = t;
})();
