// Дашборд: карта Львова з підприємствами + підказки для смуг воронки.
(function () {
  "use strict";
  var LVIV = [49.8397, 24.0297];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function plural(n, one, few, many) {
    var a = Math.abs(n) % 100, b = a % 10;
    if (b === 1 && a !== 11) return one;
    if (b >= 2 && b <= 4 && (a < 12 || a > 14)) return few;
    return many;
  }

  function days(n) { return n + " " + plural(n, "день", "дні", "днів"); }

  // Рядок вакансії в підказці: назва + дні, кандидати (нових), співбесіди, вийшли / треба.
  function vacancyRow(v) {
    return "<tr>" +
      '<th scope="row">' + esc(v.title) + (v.status !== "open" ? ' <span class="tip-paused">(' + esc(v.status_label.toLowerCase()) + ")</span>" : "") + "</th>" +
      "<td>" + days(v.days_active) + "</td>" +
      "<td>" + v.total + (v.to_call ? ' <span class="tip-muted">+' + v.to_call + "</span>" : "") + "</td>" +
      "<td>" + v.interviews + "</td>" +
      "<td>" + v.hired + "/" + v.headcount + "</td>" +
      "</tr>";
  }

  var TIP_MAX_VACANCIES = 4;

  function tooltipHtml(c) {
    var shown = c.vacancies.slice(0, TIP_MAX_VACANCIES);
    var more = c.vacancies.length - shown.length;
    return '<div class="tip-company">' + esc(c.name) + "</div>" +
      (c.address ? '<div class="tip-address">' + esc(c.address) + "</div>" : "") +
      '<table class="tip-table"><thead><tr><th>Вакансія</th><th>Активна</th><th>Канд.</th><th>Співб.</th><th>Вийшли</th></tr></thead><tbody>' +
      shown.map(vacancyRow).join("") + "</tbody></table>" +
      (more > 0 ? '<div class="tip-more">…і ще ' + more + " " + plural(more, "вакансія", "вакансії", "вакансій") + "</div>" : "") +
      '<div class="tip-hint">«+N» — нові, кому треба зателефонувати. ' +
      (c.vacancies.length === 1 ? "Клацніть, щоб відкрити вакансію." : "Клацніть, щоб вибрати вакансію.") + "</div>";
  }

  function popupHtml(c) {
    return '<div class="tip-company">' + esc(c.name) + "</div><ul class=\"popup-links\">" +
      c.vacancies.map(function (v) {
        return '<li><a href="' + esc(v.url) + '">' + esc(v.title) + "</a> · " + v.total + " канд.</li>";
      }).join("") +
      '</ul><a class="small" href="' + esc(c.edit_url) + '">Змінити розташування</a>';
  }

  // --- Карта ---
  var mapEl = document.getElementById("dash-map");
  var dataEl = document.getElementById("map-data");
  if (window.L && mapEl && dataEl) {
    var companies = JSON.parse(dataEl.textContent);
    var map = L.map(mapEl, { scrollWheelZoom: false }).setView(LVIV, 12);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map);
    map.on("focus", function () { map.scrollWheelZoom.enable(); });
    map.on("blur", function () { map.scrollWheelZoom.disable(); });

    var points = [];
    var markers = [];
    // Підказка відкривається в той бік, де більше місця, щоб не обрізатися краєм карти.
    function placeTooltips() {
      var size = map.getSize();
      markers.forEach(function (m) {
        var tooltip = m.getTooltip();
        var p = map.latLngToContainerPoint(m.getLatLng());
        var half = m.options.icon.options.iconSize[1] / 2;
        var need = m._tipHeight || 160;
        var above = p.y - half, below = size.y - p.y - half;
        var dir;
        if (above >= need) dir = "top";
        else if (below >= need) dir = "bottom";
        else dir = p.x > size.x / 2 ? "left" : "right";  // мало місця згори й знизу — збоку
        tooltip.options.direction = dir;
        tooltip.options.offset = dir === "top" ? [0, -half] : dir === "bottom" ? [0, half] : dir === "left" ? [-half, 0] : [half, 0];
      });
    }
    companies.forEach(function (c) {
      var n = c.vacancies.length;
      var size = 28 + Math.min(n - 1, 4) * 4;
      var icon = L.divIcon({
        className: "co-marker" + (c.open_count ? "" : " paused"),
        html: "<span>" + n + "</span>",
        iconSize: [size, size],
      });
      var marker = L.marker([c.lat, c.lng], { icon: icon, keyboard: true, title: c.name, alt: c.name, riseOnHover: true }).addTo(map);
      marker.bindTooltip(tooltipHtml(c), { direction: "top", offset: [0, -size / 2], className: "co-tip", opacity: 1 });
      if (n === 1) {
        marker.on("click", function () { window.location.href = c.vacancies[0].url; });
        marker.on("keypress", function (e) { if (e.originalEvent.key === "Enter") window.location.href = c.vacancies[0].url; });
      } else {
        marker.bindPopup(popupHtml(c));
        marker.on("popupopen", function () { marker.closeTooltip(); });
      }
      // Приблизна висота підказки: шапка + рядок на кожну вакансію.
      marker._tipHeight = 110 + 26 * Math.min(n, TIP_MAX_VACANCIES) + (n > TIP_MAX_VACANCIES ? 20 : 0);
      points.push([c.lat, c.lng]);
      markers.push(marker);
    });
    if (points.length > 1) map.fitBounds(points, { padding: [48, 48], maxZoom: 14 });
    else if (points.length === 1) map.setView(points[0], 14);
    placeTooltips();
    map.on("moveend zoomend resize", placeTooltips);
  }

  // --- Підказки для смуг воронки ---
  var tip = document.getElementById("viz-tip");
  function showTip(el) {
    tip.textContent = el.getAttribute("data-tip");
    tip.hidden = false;
    var r = el.getBoundingClientRect();
    var top = r.top + window.scrollY - tip.offsetHeight - 8;
    var left = r.left + window.scrollX + r.width / 2 - tip.offsetWidth / 2;
    left = Math.max(8, Math.min(left, document.documentElement.clientWidth - tip.offsetWidth - 8));
    tip.style.top = top + "px";
    tip.style.left = left + "px";
  }
  function hideTip() { tip.hidden = true; }
  document.querySelectorAll("[data-tip]").forEach(function (el) {
    el.addEventListener("mouseenter", function () { showTip(el); });
    el.addEventListener("focus", function () { showTip(el); });
    el.addEventListener("mouseleave", hideTip);
    el.addEventListener("blur", hideTip);
  });
})();
