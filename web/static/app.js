(() => {
  const { createApp, ref, computed, onMounted, nextTick } = Vue;

  function formatSize(n) {
    if (n < 1048576) return `${Math.max(1, Math.round(n / 1024))} KB`;
    if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
    return `${(n / 1073741824).toFixed(2)} GB`;
  }

  function mediaUrl(relpath) {
    return `/media?path=${encodeURIComponent(relpath)}`;
  }

  function thumbUrl(v) {
    return `/thumb?path=${encodeURIComponent(v.relpath)}`;
  }

  function bindLightDismiss(dialog) {
    if ("closedBy" in HTMLDialogElement.prototype) return;
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      const inside =
        rect.top <= event.clientY &&
        event.clientY <= rect.top + rect.height &&
        rect.left <= event.clientX &&
        event.clientX <= rect.left + rect.width;
      if (!inside) dialog.close();
    });
  }

  createApp({
    setup() {
      const phase = ref("boot");
      const authRequired = ref(false);
      const password = ref("");
      const busy = ref(false);
      const error = ref("");
      const loadError = ref("");
      const items = ref([]);
      const categories = ref([]);
      const category = ref("");
      const query = ref("");
      const current = ref(null);
      const playerEl = ref(null);
      const videoEl = ref(null);

      const totalAll = computed(() =>
        categories.value.reduce((n, c) => n + c.count, 0)
      );

      const visible = computed(() => {
        const q = query.value.trim().toLowerCase();
        return items.value.filter((v) => {
          if (category.value && v.category !== category.value) return false;
          if (!q) return true;
          return (
            v.title.toLowerCase().includes(q) ||
            v.relpath.toLowerCase().includes(q)
          );
        });
      });

      const grouped = computed(() => {
        const map = new Map();
        for (const v of visible.value) {
          const key = v.date || "未标日期";
          if (!map.has(key)) map.set(key, []);
          map.get(key).push(v);
        }
        const rows = [...map.entries()].map(([date, groupItems]) => ({
          date,
          items: groupItems,
        }));
        rows.sort((a, b) => {
          if (a.date === "未标日期") return 1;
          if (b.date === "未标日期") return -1;
          return a.date < b.date ? 1 : a.date > b.date ? -1 : 0;
        });
        return rows;
      });

      async function fetchJSON(url, options) {
        const res = await fetch(url, {
          credentials: "same-origin",
          ...options,
        });
        let data = {};
        try {
          data = await res.json();
        } catch {
          data = {};
        }
        if (!res.ok) {
          const detail = data.detail;
          const msg =
            typeof detail === "string"
              ? detail
              : Array.isArray(detail) && detail[0] && detail[0].msg
                ? detail[0].msg
                : res.statusText || "请求失败";
          const err = new Error(msg);
          err.status = res.status;
          throw err;
        }
        return data;
      }

      async function loadLibrary() {
        const data = await fetchJSON("/api/library");
        items.value = data.items || [];
        categories.value = data.categories || [];
      }

      async function boot() {
        try {
          const meta = await fetchJSON("/api/meta");
          authRequired.value = !!meta.auth_required;
          if (!authRequired.value) {
            await loadLibrary();
            phase.value = "library";
            openFromQuery();
            return;
          }
          try {
            await loadLibrary();
            phase.value = "library";
            openFromQuery();
          } catch (err) {
            if (err.status === 401) {
              phase.value = "login";
              return;
            }
            loadError.value = err.message || "片库加载失败";
            phase.value = "library";
          }
        } catch (err) {
          error.value = err.message || "连不上片库";
          phase.value = "login";
        }
      }

      async function login() {
        busy.value = true;
        error.value = "";
        try {
          await fetchJSON("/api/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password: password.value }),
          });
          password.value = "";
          await loadLibrary();
          phase.value = "library";
          openFromQuery();
        } catch (err) {
          error.value = err.message || "口令不对";
        } finally {
          busy.value = false;
        }
      }

      async function logout() {
        await fetchJSON("/api/logout", { method: "POST" });
        items.value = [];
        phase.value = "login";
      }

      function setCategory(name) {
        category.value = name;
      }

      function onThumbError(event) {
        const img = event.target;
        const path = new URL(img.src, window.location.origin).searchParams.get("path");
        const n = Number(img.dataset.tries || 0);
        if (path && n < 6) {
          img.dataset.tries = String(n + 1);
          window.setTimeout(() => {
            img.src = `${thumbUrl({ relpath: path })}&r=${Date.now()}`;
          }, 2500 * (n + 1));
          return;
        }
        img.classList.add("is-missing");
      }

      function open(v) {
        current.value = v;
        const url = new URL(window.location.href);
        url.searchParams.set("v", v.relpath);
        history.replaceState(null, "", url);
        nextTick(() => {
          const dialog = playerEl.value;
          const video = videoEl.value;
          if (!dialog || !video) return;
          video.src = mediaUrl(v.relpath);
          if (!dialog.open) dialog.showModal();
          video.play().catch(() => {});
        });
      }

      function onPlayerClose() {
        const video = videoEl.value;
        if (video) {
          video.pause();
          video.removeAttribute("src");
          video.load();
        }
        current.value = null;
        const url = new URL(window.location.href);
        url.searchParams.delete("v");
        history.replaceState(null, "", url);
      }

      function openFromQuery() {
        const rel = new URLSearchParams(window.location.search).get("v");
        if (!rel) return;
        const found = items.value.find((v) => v.relpath === rel);
        if (found) open(found);
      }

      onMounted(() => {
        if (playerEl.value) bindLightDismiss(playerEl.value);
        boot();
      });

      return {
        phase,
        authRequired,
        password,
        busy,
        error,
        loadError,
        items,
        categories,
        category,
        query,
        current,
        playerEl,
        videoEl,
        totalAll,
        visible,
        grouped,
        formatSize,
        thumbUrl,
        onThumbError,
        login,
        logout,
        setCategory,
        open,
        onPlayerClose,
      };
    },
  }).mount("#app");
})();
