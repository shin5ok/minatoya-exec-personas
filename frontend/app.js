/* ミナトヤ 幹部シミュレーター — Web UI */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };

  const PHASE_LABEL = {
    waiting: "挨拶待ち", smalltalk: "雑談", discussion: "本題", wrapup: "まとめ", closing: "結論", ended: "終了", cancelled: "キャンセル",
  };
  const DECISION_LABEL = {
    undecided: "未決", approved: "承認", conditional: "条件付き承認", rejected: "非承認", redo: "やり直し", go: "Goサイン",
  };
  const IDLE_MS = { dialog: 25000, meeting: 20000 };

  const app = {
    config: null,
    personas: {},
    selected: [],
    sessionId: null,
    state: null,
    ws: null,
    clockOffset: 0,
    timerHandle: null,
    idleHandle: null,
    talkTimers: {},
    streaming: {},
    voice: null, // { kind: 'live'|'transcribe', ws, mic, player }
  };

  // ------------------------------------------------------------------ 開始画面
  async function init() {
    app.config = await (await fetch("/api/config")).json();
    app.config.personas.forEach((p) => (app.personas[p.id] = p));
    $("#user-name-label").textContent =
      app.config.user && app.config.user !== "local-user" ? app.config.user : "ゲスト（local-user）";
    $("#user-badge").addEventListener("click", toggleHistory);
    $("#history-close").addEventListener("click", () => ($("#history").hidden = true));
    $("#loading-cancel").addEventListener("click", cancelLoading);
    $("#cancel-btn").addEventListener("click", cancelSession);
    $("#finish-btn").addEventListener("click", finishSession);
    renderPersonaGrid();
    renderDurations();
    renderThemeChips();
    $("#start-btn").addEventListener("click", startSession);
    $("#send-btn").addEventListener("click", sendInput);
    $("#input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendInput(); }
      resetIdle();
    });
    $("#input").addEventListener("input", autoGrow);
    $("#file-input").addEventListener("change", uploadFile);
    $("#mic-btn").addEventListener("click", toggleVoice);
    $("#panel-btn").addEventListener("click", () => ($("#panel").hidden = !$("#panel").hidden));
    $("#panel-close").addEventListener("click", () => ($("#panel").hidden = true));
    $("#verdict-close").addEventListener("click", () => ($("#verdict").hidden = true));
    // ?session= を外してトップへ戻る（reload だと同じセッションを再開してしまう）
    $("#restart-btn").addEventListener("click", () => location.assign("/"));
    const resume = new URLSearchParams(location.search).get("session");
    if (resume) resumeSession(resume);
  }

  function renderPersonaGrid() {
    const grid = $("#persona-grid");
    const tpl = $("#tpl-persona");
    Object.values(app.personas).forEach((p) => {
      const node = tpl.content.firstElementChild.cloneNode(true);
      node.style.setProperty("--c", p.color);
      node.dataset.id = p.id;
      node.querySelector(".avatar").src = p.avatar;
      node.querySelector(".avatar").alt = p.name;
      node.querySelector(".persona-name").textContent = p.name;
      node.querySelector(".persona-role").textContent = p.role_label;
      node.querySelector(".persona-tagline").textContent = p.tagline;
      node.addEventListener("click", () => togglePersona(p.id, node));
      grid.appendChild(node);
    });
  }

  function togglePersona(id, node) {
    const i = app.selected.indexOf(id);
    if (i >= 0) app.selected.splice(i, 1);
    else app.selected.push(id);
    node.classList.toggle("selected", i < 0);
    const pill = $("#mode-pill");
    const n = app.selected.length;
    pill.className = "mode-pill " + (n === 0 ? "mode-none" : n === 1 ? "mode-dialog" : "mode-meeting");
    pill.textContent = n === 0 ? "キャラクターを選んでください"
      : n === 1 ? `対話モード（${app.personas[app.selected[0]].name}と1対1）`
      : `ミーティングモード（${n}名: ${app.selected.map((s) => app.personas[s].name).join("・")}）`;
    $("#start-btn").disabled = n === 0;
  }

  function renderDurations() {
    const sel = $("#duration");
    const max = app.config.max_duration_minutes;
    const opts = [0, 1, 2, 3, 5, 10, 15, 30].filter((m) => m <= max);
    if (!opts.includes(max)) opts.push(max);
    opts.forEach((m) => {
      const o = el("option", null, m === 0 ? "制限なし" : `${m}分`);
      o.value = m;
      sel.appendChild(o);
    });
    sel.value = String(Math.min(5, max));
  }

  function renderThemeChips() {
    const box = $("#theme-chips");
    (app.config.issues || []).forEach((iss) => {
      const c = el("button", "chip", iss.title);
      c.type = "button";
      c.addEventListener("click", () => { $("#theme").value = iss.title; });
      box.appendChild(c);
    });
  }

  // ------------------------------------------------------------------ 準備（ローディング）
  async function startSession() {
    const body = {
      persona_ids: app.selected.slice(),
      theme: $("#theme").value.trim(),
      duration_minutes: Number($("#duration").value),
      user_name: $("#user-name").value.trim(),
      user_role: $("#user-role").value.trim(),
    };
    showLoading(body.persona_ids);
    app.prepAbort = new AbortController();
    try {
      const res = await fetch("/api/sessions", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        signal: app.prepAbort.signal,
      });
      if (!res.ok) throw new Error(await res.text());
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (line) onPrepEvent(JSON.parse(line));
        }
      }
    } catch (e) {
      hideLoading();
      if (e.name === "AbortError") return; // キャンセル
      alert("準備に失敗しました: " + e.message);
    }
  }

  // ---- キャンセル
  function cancelLoading() {
    if (app.prepAbort) app.prepAbort.abort();
    hideLoading();
    location.assign("/");
  }

  async function cancelSession() {
    const ended = app.state && (app.state.phase === "ended" || app.state.phase === "cancelled");
    if (!ended && !confirm("相談をやめてトップに戻りますか？（ここまでの内容はサマリとして残ります）")) return;
    app.leaving = true;
    stopVoice();
    try { if (app.ws) app.ws.close(); } catch (_) {}
    if (!ended && app.sessionId) {
      try { await fetch(`/api/sessions/${app.sessionId}/cancel`, { method: "POST" }); } catch (_) {}
    }
    location.assign("/");
  }

  // ---- 時間前に結論が出たときの終了
  function finishSession() {
    if (!app.ws || app.ws.readyState !== 1) return;
    app.finishing = true;
    $("#finish-btn").hidden = true;
    stopVoice();
    sysNote("🏁 会を締めています…", true);
    showThinking([app.state.chair || app.state.persona_ids[0]]);
    app.ws.send(JSON.stringify({ type: "finish" }));
  }

  // ---- これまでの相談
  async function toggleHistory() {
    const panel = $("#history");
    if (!panel.hidden) { panel.hidden = true; return; }
    panel.hidden = false;
    const body = $("#history-body");
    body.innerHTML = "";
    body.appendChild(el("div", "hist-empty", "読み込み中…（サマリを作っています）"));
    try {
      const data = await (await fetch("/api/sessions")).json();
      renderHistory(data.items || []);
    } catch (e) {
      body.innerHTML = "";
      body.appendChild(el("div", "hist-empty", "一覧を取得できませんでした: " + e.message));
    }
  }

  function renderHistory(items) {
    const body = $("#history-body");
    body.innerHTML = "";
    if (!items.length) { body.appendChild(el("div", "hist-empty", "まだ相談はありません")); return; }
    items.forEach((it) => {
      const card = el("div", "hist-item");
      const top = el("div", "hist-top");
      const avs = el("div", "hist-avatars");
      it.persona_ids.forEach((pid) => {
        const p = app.personas[pid];
        if (!p) return;
        const img = el("img");
        img.src = p.avatar; img.alt = p.name; img.title = p.name;
        img.style.setProperty("--c", p.color);
        avs.appendChild(img);
      });
      top.appendChild(avs);
      const d = new Date((it.updated_at || 0) * 1000);
      top.appendChild(el("span", "hist-date", d.toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })));
      top.appendChild(el("span", "hist-status " + ((it.verdict && it.verdict.status) || ""), it.status));
      card.appendChild(top);
      const s = it.summary;
      card.appendChild(el("div", "hist-title", (s && s.title) || it.theme || "（テーマ未設定）"));
      const text = el("div", "hist-text");
      if (s) {
        text.appendChild(el("div", null, s.consulted));
        const ul = el("ul");
        (s.key_points || []).forEach((k) => ul.appendChild(el("li", null, k)));
        text.appendChild(ul);
        text.appendChild(el("div", null, "→ " + s.outcome));
        if (s.homework && s.homework.length) text.appendChild(el("div", null, "宿題: " + s.homework.join("／")));
      } else {
        text.appendChild(el("div", null, it.phase === "waiting" || !it.phase ? "会話は始まっていません" : "サマリは会話の終了後に作られます"));
      }
      card.appendChild(text);
      card.addEventListener("click", () => location.assign(`/?session=${encodeURIComponent(it.session_id)}`));
      body.appendChild(card);
    });
  }

  function showLoading(ids) {
    const av = $("#loading-avatars");
    av.innerHTML = "";
    ids.forEach((id) => {
      const wrap = el("span");
      wrap.dataset.id = id;
      const img = el("img");
      img.src = app.personas[id].avatar;
      img.style.setProperty("--c", app.personas[id].color);
      wrap.appendChild(img);
      av.appendChild(wrap);
    });
    $("#loading-steps").innerHTML = "";
    $("#loading-docs").innerHTML = "";
    $("#loading").hidden = false;
  }
  function hideLoading() { $("#loading").hidden = true; }

  function onPrepEvent(ev) {
    const steps = $("#loading-steps");
    if (ev.type === "progress") {
      steps.querySelectorAll("li:not(.done)").forEach((li) => li.classList.add("done"));
      const li = el("li", ev.step === "ready" ? "done" : "", ev.label);
      steps.appendChild(li);
      if (ev.documents) {
        const box = $("#loading-docs");
        ev.documents.forEach((t, i) => {
          const c = el("span", "doc-chip", "📄 " + t);
          c.style.animationDelay = `${i * 0.08}s`;
          box.appendChild(c);
        });
      }
      if (ev.persona) {
        const s = $(`#loading-avatars span[data-id="${ev.persona}"]`);
        if (s) s.classList.add("done");
      }
    } else if (ev.type === "session") {
      app.sessionId = ev.session_id;
      history.replaceState(null, "", `?session=${ev.session_id}`);
      setTimeout(() => { hideLoading(); enterChat(ev.state, []); }, 600);
    } else if (ev.type === "error") {
      hideLoading();
      alert(ev.message);
    }
  }

  async function resumeSession(id) {
    try {
      const res = await fetch(`/api/sessions/${id}`);
      if (!res.ok) throw new Error("not found");
      const data = await res.json();
      app.sessionId = id;
      enterChat(data.state, data.messages);
    } catch (_) {
      history.replaceState(null, "", "/");
    }
  }

  // ------------------------------------------------------------------ 会話画面
  function enterChat(state, messages) {
    $("#screen-start").hidden = true;
    $("#screen-chat").hidden = false;
    app.state = state;
    syncClock(state);
    const stage = $("#stage-avatars");
    stage.innerHTML = "";
    state.persona_ids.forEach((id) => {
      const p = app.personas[id];
      const box = el("div", "stage-avatar");
      box.dataset.id = id;
      box.style.setProperty("--c", p.color);
      const img = el("img");
      img.src = p.avatar;
      img.alt = p.name;
      box.appendChild(img);
      box.appendChild(el("div", "nm", `${p.name}（${p.role_label}）`));
      stage.appendChild(box);
      new Image().src = p.avatar_talk; // 口パク用の画像を先読み
    });
    const mode = $("#chat-mode");
    mode.className = "mode-pill " + (state.mode === "dialog" ? "mode-dialog" : "mode-meeting");
    mode.textContent = state.mode === "dialog" ? "対話モード" : "ミーティングモード";
    $("#chat-theme").textContent = "テーマ: " + (state.theme || state.theme_summary || "（自由に相談）");
    $("#mic-btn").hidden = !app.config.voice_input_enabled;
    $("#mic-btn").title = state.mode === "dialog" ? "音声で話す（Gemini Live）" : "音声入力（Gemini Live で文字起こし）";
    renderPanel(state);
    messages.forEach((m) => addMessage(m.speaker, m.text, m.id));
    if (!messages.length) {
      sysNote(state.mode === "dialog"
        ? `${app.personas[state.persona_ids[0]].name}さんが待っています。まずは挨拶をしてみましょう。`
        : "役員がそろいました。まずは挨拶をしてみましょう。");
    }
    applyState(state);
    if (state.phase === "ended" || state.phase === "cancelled") { onEnded(state); return; }
    connectChat();
    startTimerLoop();
    $("#input").focus();
  }

  function renderPanel(state) {
    const body = $("#panel-body");
    body.innerHTML = "";
    if (state.issue_titles && state.issue_titles.length) {
      body.appendChild(el("h4", null, "関係する論点"));
      state.issue_titles.forEach((t) => body.appendChild(el("div", "doc", "🧭 " + t)));
    }
    if (state.decision_question) {
      body.appendChild(el("h4", null, "決めたいこと"));
      body.appendChild(el("div", "doc", state.decision_question));
    }
    body.appendChild(el("h4", null, "読み込んだ資料（OKF バンドル）"));
    (state.documents || []).forEach((d) => {
      const div = el("div", "doc");
      div.appendChild(el("div", null, "📄 " + d.title));
      div.appendChild(el("small", null, `${d.type} — ${d.id}`));
      body.appendChild(div);
    });
    if (state.attachments && state.attachments.length) {
      body.appendChild(el("h4", null, "提出した添付資料"));
      state.attachments.forEach((a) => {
        const div = el("div", "doc");
        div.appendChild(el("div", null, "📎 " + a.name));
        div.appendChild(el("small", null, a.digest || ""));
        body.appendChild(div);
      });
    }
    if (state.prep_seconds) body.appendChild(el("small", null, `準備にかかった時間: ${state.prep_seconds}秒`));
  }

  function connectChat() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/sessions/${app.sessionId}`);
    app.ws = ws;
    ws.onmessage = (e) => onServerEvent(JSON.parse(e.data));
    ws.onclose = () => {
      if (!app.leaving && app.state && app.state.phase !== "ended" && app.state.phase !== "cancelled") setTimeout(connectChat, 1500);
    };
  }

  function onServerEvent(ev) {
    switch (ev.type) {
      case "state": applyState(ev.state); break;
      case "plan": showThinking(ev.turns.map((t) => t.speaker)); break;
      case "delta": onDelta(ev); break;
      case "message": onMessage(ev); break;
      case "idle_ok": showThinking([]); resetIdle(); break;
      case "ended": applyState(ev.state); onEnded(ev.state); break;
      case "error": sysNote("⚠️ " + ev.message, true); showThinking([]); app.finishing = false; break;
      default: break;
    }
  }

  function applyState(state) {
    const prev = app.state || {};
    app.state = Object.assign({}, app.state, state);
    syncClock(state);
    $("#phase-pill").textContent = PHASE_LABEL[state.phase] || state.phase || "";
    const st = (state.decision && state.decision.status) || "undecided";
    const dp = $("#decision-pill");
    dp.className = "decision-pill " + st;
    dp.textContent = "判断: " + (DECISION_LABEL[st] || st);
    const closed = state.phase === "ended" || state.phase === "cancelled" || state.phase === "closing";
    $("#finish-btn").hidden = st === "undecided" || closed || !!app.finishing;
    if (st !== "undecided" && (prev.decision || {}).status !== st && !closed) {
      sysNote(`判断が出ました（${DECISION_LABEL[st] || st}）。「🏁 結論を出して終了」で会を締められます`);
    }
    if (state.phase === "wrapup" && prev.phase !== "wrapup") sysNote("⏰ 残り約1分 — まとめに入りましょう", true);
    if (state.phase === "closing" && prev.phase !== "closing") sysNote("⏱ 時間切れ直前 — 結論が出ます", true);
    if (JSON.stringify(state.attachments || []) !== JSON.stringify(prev.attachments || [])) renderPanel(app.state);
  }

  function syncClock(state) {
    if (state && state.server_time) app.clockOffset = state.server_time * 1000 - Date.now();
  }

  function startTimerLoop() {
    clearInterval(app.timerHandle);
    const tick = () => {
      const s = app.state || {};
      const total = s.duration_sec || 0;
      const t = $("#timer");
      const fill = $("#timer-fill");
      if (!total) { t.textContent = "∞"; t.className = "timer"; fill.style.width = "100%"; return; }
      let left = total;
      if (s.deadline) left = Math.max(0, s.deadline - (Date.now() + app.clockOffset) / 1000);
      if (s.phase === "ended") left = 0;
      const m = Math.floor(left / 60), sec = Math.floor(left % 60);
      t.textContent = s.deadline || s.phase === "ended" ? `${m}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}（挨拶で開始）`;
      t.className = "timer" + (left <= 10 && s.deadline ? " danger" : left <= 60 && s.deadline ? " warn" : "");
      fill.style.width = `${(left / total) * 100}%`;
    };
    tick();
    app.timerHandle = setInterval(tick, 250);
  }

  // ---- メッセージ表示
  function bubbleFor(speaker, id) {
    let node = id ? document.getElementById(id) : null;
    if (node) return node;
    const isUser = speaker === "user";
    const p = app.personas[speaker];
    node = el("div", "msg" + (isUser ? " user" : ""));
    if (id) node.id = id;
    node.style.setProperty("--c", isUser ? "var(--accent2)" : p.color);
    if (!isUser) {
      const av = el("img", "av");
      av.src = p.avatar;
      node.appendChild(av);
    }
    const inner = el("div");
    inner.appendChild(el("div", "who", isUser ? (app.state.user_name || "あなた") : `${p.name}（${p.role_label}）`));
    inner.appendChild(el("div", "bubble"));
    node.appendChild(inner);
    $("#chat-log").appendChild(node);
    return node;
  }

  function addMessage(speaker, text, id, extraCls) {
    const node = bubbleFor(speaker, id);
    if (extraCls) node.classList.add(extraCls);
    node.querySelector(".bubble").textContent = text;
    scrollDown();
    return node;
  }

  function onDelta(ev) {
    const prev = app.streaming[ev.id] || "";
    const text = ev.text.startsWith(prev) ? ev.text : prev + ev.text;
    app.streaming[ev.id] = text;
    const node = addMessage(ev.speaker, text, ev.id);
    node.classList.add("streaming");
    startTalking(ev.speaker);
    showThinking((app.thinking || []).filter((s) => s !== ev.speaker));
  }

  function onMessage(ev) {
    delete app.streaming[ev.id];
    const node = addMessage(ev.speaker, ev.text, ev.id);
    node.classList.remove("streaming");
    if (ev.speaker !== "user") {
      startTalking(ev.speaker, Math.min(4000, 60 * ev.text.length));
    }
  }

  function sysNote(text, alertCls) {
    $("#chat-log").appendChild(el("div", "sys-note" + (alertCls ? " alert" : ""), text));
    scrollDown();
  }

  function scrollDown() { const log = $("#chat-log"); log.scrollTop = log.scrollHeight; }

  function showThinking(ids) {
    app.thinking = ids;
    document.querySelectorAll(".stage-avatar").forEach((b) => b.classList.toggle("thinking", ids.includes(b.dataset.id)));
    const t = $("#typing");
    if (!ids.length) { t.hidden = true; return; }
    t.hidden = false;
    t.textContent = ids.map((id) => app.personas[id].name).join("、") + " が話そうとしています…";
  }

  // ---- 口パク（talk 画像と通常画像を交互に表示）
  function startTalking(id, ms) {
    const box = document.querySelector(`.stage-avatar[data-id="${id}"]`);
    if (!box) return;
    const img = box.querySelector("img");
    const p = app.personas[id];
    box.classList.add("speaking");
    clearInterval(app.talkTimers[id]?.flip);
    clearTimeout(app.talkTimers[id]?.stop);
    let open = false;
    const flip = setInterval(() => { open = !open; img.src = open ? p.avatar_talk : p.avatar; }, 140);
    const stop = setTimeout(() => stopTalking(id), ms || 900);
    app.talkTimers[id] = { flip, stop };
  }
  function stopTalking(id) {
    const box = document.querySelector(`.stage-avatar[data-id="${id}"]`);
    const t = app.talkTimers[id];
    if (t) { clearInterval(t.flip); clearTimeout(t.stop); }
    if (box) { box.classList.remove("speaking"); box.querySelector("img").src = app.personas[id].avatar; }
  }

  // ---- 入力
  function autoGrow() {
    const t = $("#input");
    t.style.height = "auto";
    t.style.height = Math.min(160, t.scrollHeight) + "px";
  }

  function sendInput() {
    const text = $("#input").value.trim();
    if (!text || !app.state || app.state.phase === "ended") return;
    $("#input").value = "";
    autoGrow();
    sendUserText(text);
  }

  function sendUserText(text) {
    if (app.voice && app.voice.kind === "live" && app.voice.ws.readyState === 1) {
      app.voice.ws.send(JSON.stringify({ type: "text", text }));
      addMessage("user", text, null, "voice");
      return;
    }
    addMessage("user", text);
    if (app.ws && app.ws.readyState === 1) app.ws.send(JSON.stringify({ type: "user_message", text }));
    showThinking(app.state.mode === "dialog" ? app.state.persona_ids : []);
    resetIdle();
  }

  function resetIdle() {
    clearTimeout(app.idleHandle);
    if (!app.state || app.state.phase === "ended" || app.voice) return;
    const ms = IDLE_MS[app.state.mode] || 25000;
    app.idleHandle = setTimeout(() => {
      if ($("#input").value.trim()) return resetIdle();
      if (app.ws && app.ws.readyState === 1) app.ws.send(JSON.stringify({ type: "idle" }));
    }, ms);
  }

  async function uploadFile(e) {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file || !app.sessionId) return;
    if (file.size > app.config.max_attachment_mb * 1024 * 1024) {
      alert(`${app.config.max_attachment_mb}MB までのファイルを添付できます`);
      return;
    }
    const note = $("#input").value.trim();
    const chip = el("span", "attach", `⏳ ${file.name} をアップロード中…`);
    $("#attach-list").appendChild(chip);
    const fd = new FormData();
    fd.append("file", file);
    fd.append("note", note);
    try {
      const res = await fetch(`/api/sessions/${app.sessionId}/attachments`, { method: "POST", body: fd });
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      const data = await res.json();
      chip.textContent = `📎 ${data.name}`;
      $("#input").value = "";
      sendUserText(`【添付】資料「${data.name}」を提出します。${note}`);
    } catch (err) {
      chip.remove();
      alert("添付に失敗しました: " + err.message);
    }
  }

  // ---- 音声（Gemini Live）
  async function toggleVoice() {
    if (app.voice) return stopVoice();
    if (!app.state || app.state.phase === "ended") return;
    const kind = app.state.mode === "dialog" ? "live" : "transcribe";
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/${kind}/${app.sessionId}`);
    ws.binaryType = "arraybuffer";
    const pid = app.state.persona_ids[0];
    const voice = { kind, ws, mic: null, player: null, liveUserId: null, liveModelId: null };
    app.voice = voice;
    clearTimeout(app.idleHandle);
    $("#mic-btn").classList.add("active");
    setVoiceStatus("接続しています…");
    if (kind === "live") {
      voice.player = new MinatoyaAudio.Player((on) => (on ? startTalking(pid, 600) : stopTalking(pid)));
      voice.player.ensure();
    }
    ws.onmessage = async (e) => {
      if (typeof e.data !== "string") { voice.player && voice.player.play(e.data); return; }
      const ev = JSON.parse(e.data);
      if (ev.type === "ready") {
        try {
          voice.mic = new MinatoyaAudio.Mic((pcm) => ws.readyState === 1 && ws.send(pcm));
          await voice.mic.start();
          setVoiceStatus(kind === "live" ? "🎙️ 話しかけてください（音声会話中。もう一度押すと終了）" : "🎙️ 話すと文字になって送信されます（もう一度押すと終了）");
        } catch (err) {
          setVoiceStatus("マイクを使えません: " + err.message);
          stopVoice();
        }
      } else if (ev.type === "input_transcript") {
        voice.liveUserId = voice.liveUserId || "lu-" + Date.now();
        addMessage("user", ev.text, voice.liveUserId, "voice");
      } else if (ev.type === "output_transcript") {
        voice.liveModelId = voice.liveModelId || "lm-" + Date.now();
        addMessage(ev.speaker, ev.text, voice.liveModelId, "voice");
      } else if (ev.type === "turn_complete") {
        voice.liveUserId = null; voice.liveModelId = null;
      } else if (ev.type === "message") {
        // 書き戻し済みの確定テキスト（表示はストリーミング分で済んでいる）
      } else if (ev.type === "interrupted") {
        voice.player && voice.player.stopAll();
      } else if (ev.type === "partial") {
        $("#input").value = ev.text;
      } else if (ev.type === "final") {
        $("#input").value = "";
        sendUserText(ev.text);
      } else if (ev.type === "state") {
        applyState(ev.state);
      } else if (ev.type === "cue") {
        if (ev.kind === "wrapup") sysNote("⏰ 残り約1分 — まとめに入りましょう", true);
        if (ev.kind === "final") sysNote("⏱ 時間切れ直前 — 結論が出ます", true);
      } else if (ev.type === "ended") {
        applyState(ev.state);
        setTimeout(() => { stopVoice(); onEnded(ev.state); }, 1500);
      } else if (ev.type === "error") {
        sysNote("⚠️ " + ev.message, true);
        stopVoice();
      }
    };
    ws.onclose = () => { if (app.voice === voice) stopVoice(); };
  }

  function stopVoice() {
    const v = app.voice;
    if (!v) return;
    app.voice = null;
    try { v.mic && v.mic.stop(); } catch (_) {}
    try { if (v.ws.readyState === 1) v.ws.send(JSON.stringify({ type: "stop" })); v.ws.close(); } catch (_) {}
    if (v.player) setTimeout(() => v.player.close(), 300);
    $("#mic-btn").classList.remove("active");
    setVoiceStatus("");
    resetIdle();
  }

  function setVoiceStatus(text) {
    const s = $("#voice-status");
    s.textContent = text;
    s.hidden = !text;
  }

  // ---- 終了
  function onEnded(state) {
    showThinking([]);
    clearTimeout(app.idleHandle);
    $("#input").disabled = true;
    $("#send-btn").disabled = true;
    $("#input").placeholder = "この会は終了しました";
    $("#cancel-btn").textContent = "← トップへ";
    $("#cancel-btn").title = "トップに戻る";
    const v = state.verdict;
    if (!v) { sysNote(state.phase === "cancelled" ? "この相談はキャンセルされました" : "会話は終了しました"); return; }
    const stamp = $("#verdict-stamp");
    stamp.className = "stamp " + v.status;
    stamp.textContent = v.label;
    const by = app.personas[v.by];
    $("#verdict-by").textContent = `${by ? by.name : ""}（${by ? by.role_label : ""}）の判断` + (v.interrupted ? " — 時間切れのため割り込みで決定" : "");
    $("#verdict-reason").textContent = v.reason || "";
    const ul = $("#verdict-conditions");
    ul.innerHTML = "";
    (v.conditions || []).forEach((c) => ul.appendChild(el("li", null, c)));
    setTimeout(() => ($("#verdict").hidden = false), 1800);
  }

  window.addEventListener("DOMContentLoaded", init);
})();
