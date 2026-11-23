// Вибір розташування підприємства на карті Львова (сторінка підприємства).
(function () {
  "use strict";
  var LVIV = [49.8397, 24.0297];
  var latInput = document.getElementById("id_latitude");
  var lngInput = document.getElementById("id_longitude");
  var addressInput = document.getElementById("id_address");
  var statusEl = document.getElementById("map-status");
  var clearBtn = document.getElementById("clear-point");
  var geocodeBtn = document.getElementById("geocode-btn");
  if (!window.L || !latInput) return;

  var map = L.map("company-map", { scrollWheelZoom: false }).setView(LVIV, 12);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    // OSM вимагає Referer, а сайт загалом шле політику same-origin (без Referer назовні).
    // Для тайлів передаємо лише адресу сайту, без шляху сторінки.
    referrerPolicy: "strict-origin-when-cross-origin",
  }).addTo(map);

  var pinIcon = L.divIcon({ className: "pin", html: "<span></span>", iconSize: [24, 24], iconAnchor: [12, 24] });
  var marker = null;

  function setStatus(text) { statusEl.textContent = text; }

  function setPoint(latlng, zoom) {
    var lat = Math.round(latlng.lat * 1e6) / 1e6;
    var lng = Math.round(latlng.lng * 1e6) / 1e6;
    latInput.value = lat.toFixed(6);
    lngInput.value = lng.toFixed(6);
    if (!marker) {
      marker = L.marker([lat, lng], { icon: pinIcon, draggable: true, keyboard: true, title: "Розташування підприємства" }).addTo(map);
      marker.on("dragend", function () { setPoint(marker.getLatLng()); });
    } else {
      marker.setLatLng([lat, lng]);
    }
    if (zoom) map.setView([lat, lng], zoom);
    clearBtn.hidden = false;
    setStatus("📍 Точку вибрано. Її можна перетягнути, якщо треба уточнити.");
  }

  function clearPoint() {
    latInput.value = "";
    lngInput.value = "";
    if (marker) { map.removeLayer(marker); marker = null; }
    clearBtn.hidden = true;
    setStatus("Точку прибрано — підприємства не буде на карті дашборду.");
  }

  if (latInput.value && lngInput.value) {
    setPoint(L.latLng(parseFloat(latInput.value), parseFloat(lngInput.value)), 15);
  } else {
    setStatus("Точку ще не вибрано.");
  }

  map.on("click", function (e) { setPoint(e.latlng); });
  clearBtn.addEventListener("click", clearPoint);

  function geocode() {
    var query = addressInput.value.trim();
    if (!query) { setStatus("Спершу впишіть адресу."); addressInput.focus(); return; }
    if (!/льв|lviv/i.test(query)) query += ", Львів";
    setStatus("Шукаю адресу…");
    geocodeBtn.disabled = true;
    var url = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&countrycodes=ua&accept-language=uk" +
      "&viewbox=23.85,49.93,24.20,49.76&q=" + encodeURIComponent(query);
    // Nominatim (як і тайли OSM) вимагає Referer — передаємо лише адресу сайту.
    fetch(url, { headers: { "Accept": "application/json" }, referrerPolicy: "strict-origin-when-cross-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (results) {
        if (!results.length) {
          setStatus("Адресу не знайдено. Клацніть на карті там, де підприємство, — точка поставиться вручну.");
          return;
        }
        setPoint(L.latLng(parseFloat(results[0].lat), parseFloat(results[0].lon)), 16);
        setStatus("📍 Знайдено: " + results[0].display_name + ". Перевірте точку й за потреби перетягніть її.");
      })
      .catch(function () {
        setStatus("Не вдалося виконати пошук (немає інтернету?). Клацніть на карті, щоб поставити точку вручну.");
      })
      .finally(function () { geocodeBtn.disabled = false; });
  }

  geocodeBtn.addEventListener("click", geocode);
  addressInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); geocode(); }
  });
})();
