// 성공 기준 행 추가. JS가 없어도 기본 행으로 폼은 동작합니다.
(function () {
  const button = document.getElementById("add-criterion");
  const template = document.getElementById("criterion-template");
  const container = document.getElementById("criteria");
  if (!button || !template || !container) return;
  const max = Number(button.dataset.max || 20);
  button.hidden = container.querySelectorAll("[data-row]").length >= max;
  button.addEventListener("click", function () {
    const index = container.querySelectorAll("[data-row]").length;
    if (index >= max) return;
    const html = template.innerHTML.replaceAll("__i__", String(index));
    const holder = document.createElement("div");
    holder.innerHTML = html;
    const row = holder.firstElementChild;
    row.querySelector("[data-row-number]").textContent = String(index + 1);
    template.before(row);
    row.querySelector("input").focus();
    if (index + 1 >= max) button.hidden = true;
  });
})();
