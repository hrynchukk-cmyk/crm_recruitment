// Дошка кандидатів: зміна статусу перетягуванням картки між колонками (на телефоні — списком на картці).
(function () {
  "use strict";
  var board = document.querySelector("[data-board]");
  if (!board || !board.hasAttribute("data-can-move")) return;

  var csrf = board.getAttribute("data-csrf");
  var dialog = document.getElementById("interview-dialog");
  var toast = document.querySelector(".board-toast");
  var dragged = null;
  var toastTimer = null;

  function column(stage) { return board.querySelector('.board-col[data-stage="' + stage + '"]'); }

  function updateCounts() {
    board.querySelectorAll(".board-col").forEach(function (col) {
      col.querySelector("[data-count]").textContent = col.querySelectorAll(".board-card").length;
    });
  }

  function showToast(text) {
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.hidden = true; }, 6000);
  }

  // Для «Запрошено на співбесіду» потрібні дата й час. Повертає значення поля або null, якщо скасували.
  function askInterviewDate(card) {
    return new Promise(function (resolve) {
      var input = dialog.querySelector("input");
      dialog.querySelector("[data-dialog-name]").textContent = card.querySelector(".board-card-name").textContent;
      input.value = "";
      dialog.returnValue = "";
      dialog.addEventListener("close", function onClose() {
        dialog.removeEventListener("close", onClose);
        resolve(dialog.returnValue === "ok" && input.value ? input.value : null);
      });
      dialog.showModal();
      input.focus();
    });
  }

  function setStageSelect(card, stage) {
    var select = card.querySelector(".board-card-stage");
    if (select) select.value = stage;
  }

  function move(card, toCol) {
    var fromCol = card.closest(".board-col");
    var from = fromCol.getAttribute("data-stage");
    var to = toCol.getAttribute("data-stage");
    if (from === to) return;

    var needDate = to === "interview" && !card.getAttribute("data-interview");
    (needDate ? askInterviewDate(card) : Promise.resolve("")).then(function (when) {
      if (when === null) { setStageSelect(card, from); return; }

      // Переносимо одразу, а якщо сервер не прийме — повертаємо на місце.
      var placeholder = document.createComment("board-card");
      card.parentNode.insertBefore(placeholder, card);
      toCol.querySelector("[data-dropzone]").prepend(card);
      setStageSelect(card, to);
      card.classList.add("saving");
      updateCounts();

      var body = new FormData();
      body.append("stage", to);
      if (when) body.append("interview_at", when);
      fetch(card.getAttribute("data-stage-url"), {
        method: "POST",
        body: body,
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrf, "X-Requested-With": "fetch" },
      })
        .then(function (response) {
          return response.json().catch(function () { return {}; }).then(function (data) {
            if (!response.ok || !data.ok) throw new Error(data.error || "");
            return data;
          });
        })
        .then(function (data) {
          placeholder.remove();
          card.classList.remove("saving");
          card.setAttribute("data-interview", data.interview_at || "");
          var line = card.querySelector(".board-card-interview");
          line.querySelector("[data-interview-text]").textContent = data.interview_display || "";
          line.hidden = !(data.stage === "interview" && data.interview_display);
          card.classList.add("moved");
          setTimeout(function () { card.classList.remove("moved"); }, 900);
        })
        .catch(function (error) {
          placeholder.parentNode.insertBefore(card, placeholder);
          placeholder.remove();
          card.classList.remove("saving");
          setStageSelect(card, from);
          updateCounts();
          showToast((error && error.message) || "Не вдалося змінити статус. Перевірте з'єднання й спробуйте ще раз.");
        });
    });
  }

  board.addEventListener("dragstart", function (event) {
    var card = event.target.closest && event.target.closest(".board-card[draggable]");
    if (!card) return;
    dragged = card;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", card.getAttribute("data-id"));
    requestAnimationFrame(function () { card.classList.add("dragging"); });
  });

  board.addEventListener("dragend", function () {
    if (dragged) dragged.classList.remove("dragging");
    dragged = null;
    board.querySelectorAll(".drop-target").forEach(function (col) { col.classList.remove("drop-target"); });
  });

  // Колонок більше, ніж вміщує екран: біля лівого чи правого краю дошка прокручується сама.
  function autoScroll(event) {
    var rect = board.getBoundingClientRect();
    var edge = Math.min(90, rect.width / 6);
    if (event.clientX > rect.right - edge) board.scrollLeft += 22;
    else if (event.clientX < rect.left + edge) board.scrollLeft -= 22;
  }

  board.addEventListener("dragover", function (event) {
    if (!dragged) return;
    event.preventDefault();
    autoScroll(event);
    var col = event.target.closest(".board-col");
    if (!col) return;
    event.dataTransfer.dropEffect = "move";
    if (!col.classList.contains("drop-target")) {
      board.querySelectorAll(".drop-target").forEach(function (other) { other.classList.remove("drop-target"); });
      col.classList.add("drop-target");
    }
  });

  board.addEventListener("dragleave", function (event) {
    var col = event.target.closest(".board-col");
    if (col && !col.contains(event.relatedTarget)) col.classList.remove("drop-target");
  });

  board.addEventListener("drop", function (event) {
    var col = dragged && event.target.closest(".board-col");
    if (!col) return;
    event.preventDefault();
    col.classList.remove("drop-target");
    move(dragged, col);
  });

  board.addEventListener("change", function (event) {
    if (!event.target.classList.contains("board-card-stage")) return;
    var card = event.target.closest(".board-card");
    var col = column(event.target.value);
    if (col) move(card, col);
  });
})();
