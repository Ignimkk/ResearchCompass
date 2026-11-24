// 인계 Markdown 복사 버튼. 스크립트가 없어도 텍스트를 선택해 복사할 수 있으므로 버튼은 스크립트가 있을 때만 보입니다.
(function () {
  "use strict";
  var button = document.getElementById("copy-handoff");
  if (!button) return;
  var source = document.getElementById(button.dataset.target);
  var status = document.getElementById("copy-status");
  button.hidden = false;

  function report(message) {
    if (status) status.textContent = message;
  }

  button.addEventListener("click", function () {
    var text = source.value;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () {
        report("복사했습니다. 새 Claude Code 세션에 붙여넣으세요.");
      }, fallback);
    } else {
      fallback();
    }

    function fallback() {
      source.focus();
      source.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      report(ok ? "복사했습니다. 새 Claude Code 세션에 붙여넣으세요." : "자동 복사를 못 했습니다. 선택된 텍스트를 Ctrl+C로 복사하세요.");
    }
  });
})();
