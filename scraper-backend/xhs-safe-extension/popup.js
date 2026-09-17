(() => {
  const $ = (id) => document.getElementById(id);

  function setValue(id, text, className = "muted") {
    const node = $(id);
    node.textContent = text;
    node.className = `value ${className}`;
  }

  function describePage(state) {
    if (!state) return ["无法读取", "muted"];
    if (state.verification_wall) return ["需要人工交互验证", "warn"];
    if (state.login_wall) return ["需要登录", "warn"];
    if (state.inaccessible) return ["页面不可访问", "bad"];
    if (state.detail_ready) return ["详情已就绪", "ok"];
    return ["等待页面就绪", "muted"];
  }

  function refresh() {
    setValue("bridge", "检测中…");
    setValue("tab", "检测中…");
    $("path").textContent = "/";
    setValue("page", "检测中…");
    chrome.runtime.sendMessage({type: "GET_STATUS"}, (status) => {
      if (chrome.runtime.lastError || !status) {
        setValue("bridge", "扩展后台不可用", "bad");
        setValue("tab", "无法检测", "muted");
        setValue("page", "请重新加载扩展", "warn");
        return;
      }
      setValue("bridge", status.bridge_connected ? "已连接" : "未连接", status.bridge_connected ? "ok" : "bad");
      setValue("tab", status.managed_tab_present ? "已识别" : "未识别", status.managed_tab_present ? "ok" : "warn");
      $("path").textContent = status.url_path || "/";
      const [pageText, pageClass] = describePage(status.page_state);
      setValue("page", pageText, pageClass);
    });
  }

  $("version").textContent = `版本 ${chrome.runtime.getManifest().version} · 受限安全模式`;
  $("refresh").addEventListener("click", refresh);
  refresh();
})();
