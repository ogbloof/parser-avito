(function () {
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }

  const API_BASE = window.API_BASE || window.location.origin;
  let initData = "";

  if (tg?.initData) {
    initData = tg.initData;
  }

  const headers = {
    "Content-Type": "application/json",
    "X-Telegram-Init-Data": initData,
  };

  async function fetchApi(path) {
    const url = path.startsWith("http") ? path : `${API_BASE}${path}`;
    const res = await fetch(url, { headers });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw { status: res.status, ...data };
    }
    return data;
  }

  async function postApi(path, body) {
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw { status: res.status, ...data };
    }
    return data;
  }

  let currentTab = "new";
  const $loading = document.getElementById("loading");
  const $error = document.getElementById("error");
  const $feed = document.getElementById("feed");

  function hideAll() {
    $loading.hidden = true;
    $error.hidden = true;
    $feed.hidden = true;
  }

  function showError(msg) {
    hideAll();
    $error.textContent = msg;
    $error.hidden = false;
  }

  function renderCard(ad) {
    const favIcon = ad.is_favorite ? "⭐" : "☆";
    const statusMap = {
      new: "🆕 Новое",
      in_work: "🔄 В работе",
      called: "📞 Позвонил",
      no_answer: "📵 Не дозвонился",
      meeting_set: "📅 Показ назначен",
      deal: "✅ Сделка",
      lost: "❌ Отказ",
      closed: "🏁 Закрыто",
    };
    const statusLabel = statusMap[ad.status_pipeline] || ad.status_pipeline || "Новое";

    const imgHtml = ad.photo_url
      ? `<img class="card-img" src="${API_BASE}${ad.photo_url}" alt="" loading="lazy" onerror="this.style.display='none'">`
      : "";

    return `
      <article class="card" data-id="${ad.id}">
        ${imgHtml}
        <div class="card-body">
          <h2 class="card-title">${escapeHtml(ad.title)}</h2>
          <div class="card-price">${escapeHtml(ad.price)}</div>
          <div class="card-address">${escapeHtml(ad.address)}</div>
          <div class="card-meta">[${ad.source.toUpperCase()}] ${statusLabel}</div>
          <div class="card-actions">
            ${currentTab === "new" ? '<button class="btn btn-primary btn-action" data-action="in_work">В работу</button>' : ""}
            <button class="btn-fav btn-fav-toggle" data-fav="${ad.is_favorite}">${favIcon}</button>
            ${currentTab === "new" ? '<button class="btn btn-secondary btn-action" data-action="skip">Дальше</button>' : ""}
          </div>
          <a href="${escapeHtml(ad.url)}" target="_blank" rel="noopener" class="card-link">Открыть на сайте</a>
        </div>
      </article>
    `;
  }

  function escapeHtml(s) {
    if (!s) return "";
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  async function loadFeed() {
    hideAll();
    $loading.hidden = false;

    try {
      await fetchApi("/api/user");

      const endpoint = currentTab === "new" ? "/api/ads/new" : currentTab === "mine" ? "/api/ads/mine" : "/api/ads/favorite";
      const { ads } = await fetchApi(endpoint);

      hideAll();
      $feed.innerHTML = ads.length ? ads.map(renderCard).join("") : "<p class='loading'>Нет объявлений</p>";
      $feed.hidden = false;

      $feed.querySelectorAll(".btn-action").forEach((btn) => {
        btn.addEventListener("click", handleAction);
      });
      $feed.querySelectorAll(".btn-fav-toggle").forEach((btn) => {
        btn.addEventListener("click", handleFavorite);
      });
    } catch (e) {
      if (e.status === 401) {
        showError("Авторизация не прошла. Откройте приложение из бота.");
      } else {
        showError("Ошибка загрузки: " + (e.error || e.status || "неизвестная"));
      }
    }
  }

  async function handleAction(ev) {
    const card = ev.target.closest(".card");
    if (!card) return;
    const id = parseInt(card.dataset.id, 10);
    const action = ev.target.dataset.action;
    if (!action) return;

    try {
      await postApi(`/api/ads/${id}/status`, { action });
      if (action === "skip" || action === "in_work") {
        card.remove();
        if (!$feed.querySelector(".card")) {
          $feed.innerHTML = "<p class='loading'>Больше нет объявлений</p>";
        }
      }
    } catch (e) {
      alert("Ошибка: " + (e.error || "попробуйте снова"));
    }
  }

  async function handleFavorite(ev) {
    ev.preventDefault();
    const card = ev.target.closest(".card");
    if (!card) return;
    const id = parseInt(card.dataset.id, 10);
    const isFav = ev.target.dataset.fav === "true";

    try {
      await postApi(`/api/ads/${id}/status`, { action: isFav ? "unfavorite" : "favorite" });
      ev.target.dataset.fav = isFav ? "false" : "true";
      ev.target.textContent = isFav ? "☆" : "⭐";
    } catch (e) {
      alert("Ошибка: " + (e.error || "попробуйте снова"));
    }
  }

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      currentTab = tab.dataset.tab;
      loadFeed();
    });
  });

  loadFeed();
})();
