/* 管理后台页面逻辑（D3-C）。按 body[data-page] 分派：
   login / changepw / groups / group-config / group-members /
   group-whitelist / group-fields / group-backfill / instances
   鉴权与 CSRF 由 admin-core.js（window.GOPS）提供。 */
(function () {
  "use strict";

  var GOPS = window.GOPS;

  function $(id) { return document.getElementById(id); }
  function on(id, ev, fn) {
    var el = $(id);
    if (el) { el.addEventListener(ev, fn); }
  }
  function groupId() {
    var raw = document.body.getAttribute("data-group-id") || "";
    try { return decodeURIComponent(raw); } catch (e) { return raw; }
  }
  function gPath(suffix) {
    return "/api/admin/groups/" + encodeURIComponent(groupId()) + suffix;
  }
  function showErr(id, text) {
    var el = $(id);
    if (!el) { return; }
    el.textContent = GOPS.plain(text, "请检查填写内容后重试");
    el.classList.remove("d-none");
  }
  function hideErr(id) {
    var el = $(id);
    if (el) { el.classList.add("d-none"); }
  }

  /* Tabler v1.x 的 UMD 包把 Bootstrap JS 挂在 window.tabler.bootstrap 上
     （不是全局 bootstrap）；个别旧版 Tabler 直接挂 window.bootstrap。
     两处都兼容，取不到时返回 null，由调用方判空。 */
  function bsGlobal() {
    if (window.tabler && window.tabler.bootstrap) { return window.tabler.bootstrap; }
    if (window.bootstrap) { return window.bootstrap; }
    return null;
  }

  // ---------- 登录 / 改密 ----------

  function initLogin() {
    var params = new URLSearchParams(location.search);
    if (params.get("changed") === "1") {
      var notice = document.createElement("div");
      notice.className = "alert alert-success mt-3";
      notice.textContent = "密码已修改，请用新密码登录";
      var form = $("login-form");
      form.querySelector(".card-body").insertBefore(notice, form.querySelector(".form-footer"));
    }
    $("login-form").addEventListener("submit", function (e) {
      e.preventDefault();
      hideErr("login-error");
      var form = $("login-form");
      if (form.classList.contains("needs-validation") &&
          !form.checkValidity()) {
        form.classList.add("was-validated");
        return;
      }
      GOPS.api("/api/admin/login", {
        method: "POST",
        body: {
          username: $("login-username").value.trim(),
          password: $("login-password").value
        }
      }).then(function (r) {
        if (r.status !== 200 || !r.data.ok) {
          showErr("login-error", r.data.detail || "登录失败");
          return;
        }
        location.href = r.data.must_change_password ? "/change-password" : "/";
      });
    });
  }

  function initChangepw() {
    $("changepw-form").addEventListener("submit", function (e) {
      e.preventDefault();
      hideErr("cpw-error");
      var newPw = $("cpw-new").value;
      if (newPw.length < 8) {
        showErr("cpw-error", "新密码长度至少 8 位");
        return;
      }
      GOPS.api("/api/admin/change-password", {
        method: "POST",
        body: { old_password: $("cpw-old").value, new_password: newPw }
      }).then(function (r) {
        if (r.status !== 200 || !r.data.ok) {
          showErr("cpw-error", r.data.detail || "修改失败");
          return;
        }
        location.href = "/login?changed=1";
      });
    });
  }

  // ---------- 仪表盘（群选择：整页重渲染，服务端按群取数） ----------

  function initDashboard() {
    on("dash-group-select", "change", function () {
      var sel = $("dash-group-select");
      if (!sel || !sel.value) { return; }
      location.href = "/?group_id=" + encodeURIComponent(sel.value);
    });
  }

  // ---------- 群管理 ----------

  function initGroups() {
    var bs = bsGlobal();
    var modalEl = $("new-group-modal");
    var modal = bs && modalEl ? new bs.Modal(modalEl) : null;
    on("new-group-btn", "click", function () {
      $("new-group-id").value = "";
      hideErr("new-group-error");
      if (modal) { modal.show(); }
      else { showErr("new-group-error", "弹窗组件未加载，请刷新页面重试"); }
    });
    on("new-group-submit", "click", function () {
      var gid = $("new-group-id").value.trim();
      if (!gid) { showErr("new-group-error", "请填写群标识"); return; }
      GOPS.api("/api/admin/groups", { method: "POST", body: { group_id: gid } })
        .then(function (r) {
          if (r.data.ok) {
            location.href = "/groups/" + encodeURIComponent(gid) + "/config";
          } else {
            showErr("new-group-error", r.data.detail || "建群失败");
          }
        });
    });
    Array.prototype.forEach.call(
      document.querySelectorAll("#discovered-tbody button[data-bind]"),
      function (btn) {
        btn.addEventListener("click", function () {
          var gid = btn.getAttribute("data-bind");
          if (!window.confirm("绑定后五项业务默认全部关闭，需手动逐项开启。确认绑定群 " + gid + "？")) {
            return;
          }
          GOPS.api("/api/admin/groups/bind", { method: "POST", body: { group_id: gid } })
            .then(function (r) {
              if (r.data.ok) { location.reload(); }
              else { window.alert(GOPS.plain(r.data.detail, "绑定失败")); }
            });
        });
      });
  }

  // ---------- 群实例绑定 ----------

  function initInstances() {
    var tbody = $("instance-tbody");
    var bs = bsGlobal();
    var modalEl = $("bind-modal");
    var modal = bs && modalEl ? new bs.Modal(modalEl) : null;
    var mode = "bind";
    var currentGid = "";

    function badge(status) {
      if (status === "ACTIVE") { return '<span class="badge bg-success">ACTIVE</span>'; }
      if (status === "DISABLED") { return '<span class="badge bg-secondary">DISABLED</span>'; }
      return '<span class="badge bg-warning">WAIT_BIND</span>';
    }

    function openModal(inst) {
      currentGid = inst.group_id;
      mode = inst.status === "WAIT_BIND" ? "bind" : "update";
      $("bind-modal-title").textContent = mode === "bind" ? "绑定推送地址" : "修改推送地址";
      $("bind-group-id").value = inst.group_id;
      $("bind-display-name").value = inst.display_name || "";
      $("bind-webhook-url").value = inst.webhook_base_url || "";
      hideErr("bind-error");
      if (modal) { modal.show(); }
      else { showErr("bind-error", "弹窗组件未加载，请刷新页面重试"); }
    }

    function load() {
      var status = $("status-filter").value;
      var path = "/api/admin/robot-instances" +
                 (status ? "?status=" + encodeURIComponent(status) : "");
      GOPS.api(path).then(function (r) {
        if (r.status !== 200) {
          tbody.innerHTML = '<tr><td colspan="7" class="text-danger text-center">加载失败</td></tr>';
          return;
        }
        var list = r.data.instances || [];
        if (!list.length) {
          tbody.innerHTML = '<tr><td colspan="7" class="text-muted text-center">' +
            '暂无实例（机器人进群后自动出现，不支持手动提前创建）</td></tr>';
          return;
        }
        tbody.innerHTML = list.map(function (i) {
          var gid = GOPS.esc(i.group_id);
          var actions;
          if (i.status === "ACTIVE") {
            actions = '<button class="btn btn-sm btn-outline-primary" data-act="edit" data-gid="' + gid + '">修改</button> ' +
                      '<button class="btn btn-sm btn-outline-danger" data-act="disable" data-gid="' + gid + '">禁用</button>';
          } else if (i.status === "DISABLED") {
            actions = '<button class="btn btn-sm btn-outline-primary" data-act="edit" data-gid="' + gid + '">修改</button> ' +
                      '<button class="btn btn-sm btn-outline-success" data-act="enable" data-gid="' + gid + '">启用</button>';
          } else {
            actions = '<button class="btn btn-sm btn-primary" data-act="edit" data-gid="' + gid + '">绑定</button>';
          }
          return "<tr>" +
            "<td class=\"mono\">" + gid + "</td>" +
            "<td>" + GOPS.esc(i.display_name || "—") + "</td>" +
            "<td>" + badge(i.status) + "</td>" +
            "<td class=\"mono\">" + GOPS.esc(i.webhook_base_url || "未绑定") + "</td>" +
            "<td>" + (i.webhook_key_set ? "<span class=\"mono\">" + GOPS.esc(i.webhook_key_masked) + "</span>" : "—") + "</td>" +
            "<td class=\"text-muted\">" + GOPS.esc(i.updated_at || "—") + "</td>" +
            "<td class=\"text-nowrap\">" + actions + "</td></tr>";
        }).join("");
        Array.prototype.forEach.call(
          tbody.querySelectorAll("button[data-act]"), function (btn) {
            btn.addEventListener("click", function () {
              var act = btn.getAttribute("data-act");
              var gid = btn.getAttribute("data-gid");
              if (act === "edit") {
                var inst = null;
                for (var i = 0; i < list.length; i++) {
                  if (list[i].group_id === gid) { inst = list[i]; break; }
                }
                if (inst) { openModal(inst); }
                return;
              }
              var label = act === "enable" ? "启用" : "禁用";
              var effect = act === "enable"
                ? "启用后恢复主动推送与文件发送能力，不影响消息回复和群内业务权限。"
                : "停用后只停止主动推送与文件发送，不影响消息回复和群内业务权限。";
              if (!window.confirm("确认" + label + "实例 " + gid + "？\n" + effect)) {
                return;
              }
              GOPS.api("/api/admin/robot-instances/" +
                       encodeURIComponent(gid) + "/" + act, { method: "POST", body: {} })
                .then(function (r) {
                  if (!r.data.ok) { window.alert(GOPS.plain(r.data.detail, "操作失败")); return; }
                  load();
                });
            });
          });
      });
    }

    on("status-filter", "change", load);
    on("bind-submit", "click", function () {
      var url = $("bind-webhook-url").value.trim();
      var name = $("bind-display-name").value.trim();
      if (!url) { showErr("bind-error", "请填写推送地址"); return; }
      var path = "/api/admin/robot-instances/" + encodeURIComponent(currentGid);
      var method = "POST";
      if (mode === "update") { method = "PUT"; }
      else { path += "/bind"; }
      GOPS.api(path, {
        method: method,
        body: { webhook_base_url: url, display_name: name || null }
      }).then(function (r) {
        if (!r.data.ok) { showErr("bind-error", r.data.detail || "操作失败"); return; }
        if (modal) { modal.hide(); }
        load();
      });
    });

    load();
  }

  // ---------- 群配置与开关 ----------

  var BIZ_LABEL = { checkin: "打卡", summary: "总结", report: "报数" };
  var PERM_LABEL = {
    ADMIN_ALL: "全部管理员操作权限"
  };

  function initGroupConfig() {
    function loadConfig() {
      GOPS.api(gPath("/config")).then(function (r) {
        if (r.status !== 200) { return; }
        var c = r.data.config;
        $("cfg-group-name").value = c.group_name || "";
        $("cfg-status").value = c.status || "active";
        $("sw-checkin").checked = !!c.switches.checkin;
        $("sw-summary").checked = !!c.switches.summary;
        $("sw-report").checked = !!c.switches.report;
        $("sw-reminder").checked = !!c.switches.reminder;
        $("sw-statistics").checked = !!c.switches.statistics;
        $("cfg-period-type").value = c.period_type;
        // ⭐ 2026-10-19：截止时间已废弃（总结 / 报数改为整天执行），
        // 页面不再有 cfg-daily-cutoff 输入框 —— 后端字段仅为存量兼容。
        renderSlotRows(c.checkin_slots || []);
        renderRuleRows(c.reminder_rules || {});
        $("cfg-require-image").checked = !!c.checkin_require_image;
      });
    }

    function renderSlotRows(slots) {
      if (!slots.length) {
        $("slot-tbody").innerHTML =
          '<tr><td colspan="4" class="text-muted text-center">暂无时间段（至少配一个）</td></tr>';
        return;
      }
      $("slot-tbody").innerHTML = slots.map(function (s, i) {
        // id 是额度键：不按行号重排；新增行发新 id，删除行不影响其他行
        var sid = s.id || ("s" + Date.now() + i);
        return '<tr data-sid="' + GOPS.esc(sid) + '">' +
          '<td><input class="form-control form-control-sm sl-name" value="' + GOPS.esc(s.name || "") + '" placeholder="如：上午场"></td>' +
          '<td><input class="form-control form-control-sm sl-start" value="' + GOPS.esc(s.start) + '" placeholder="08:00"></td>' +
          '<td><input class="form-control form-control-sm sl-end" value="' + GOPS.esc(s.end) + '" placeholder="09:30"></td>' +
          '<td><button class="btn btn-sm btn-outline-danger" type="button">删除</button></td></tr>';
      }).join("");
      Array.prototype.forEach.call(
        $("slot-tbody").querySelectorAll("tr[data-sid]"), function (tr) {
          var btn = tr.querySelector("button");
          if (!btn) { return; }
          btn.addEventListener("click", function () {
            tr.remove();
            if (!$("slot-tbody").querySelectorAll("tr[data-sid]").length) {
              renderSlotRows([]);
            }
          });
        });
    }

    function collectSlots() {
      var rows = $("slot-tbody").querySelectorAll("tr[data-sid]");
      var slots = [];
      for (var i = 0; i < rows.length; i++) {
        slots.push({
          id: rows[i].getAttribute("data-sid"),
          name: rows[i].querySelector(".sl-name").value.trim(),
          start: rows[i].querySelector(".sl-start").value.trim(),
          end: rows[i].querySelector(".sl-end").value.trim()
        });
      }
      return slots;
    }

    function renderRuleRows(rules) {
      // 2026-10-16 用户指令三：打卡提醒时间固定 = 时段开始时间（不可配），
      // 只留开关 + 手动提醒按钮；总结 / 报数保留时间与文本配置。
      $("rr-tbody").innerHTML = ["checkin", "summary", "report"].map(function (biz) {
        var r = rules[biz] || { enabled: true, times: [], text: "" };
        var fixed = biz === "checkin";
        var timeCell = fixed
          ? '<span class="text-muted">时段开始时间（固定）</span>'
          : '<input class="form-control form-control-sm rr-times" data-biz="' + biz + '" value="' + GOPS.esc((r.times || []).join(", ")) + '" placeholder="09:00, 14:50">';
        var textCell = fixed
          ? '<span class="text-muted">默认文案</span>'
          : '<input class="form-control form-control-sm rr-text" data-biz="' + biz + '" value="' + GOPS.esc(r.text || "") + '" placeholder="留空 = 默认文案">';
        return "<tr><td>" + BIZ_LABEL[biz] + "</td>" +
          '<td><input class="form-check-input rr-on" type="checkbox" data-biz="' + biz + '"' + (r.enabled ? " checked" : "") + "></td>" +
          "<td>" + timeCell + "</td>" +
          "<td>" + textCell + "</td>" +
          '<td><button class="btn btn-sm btn-outline-primary rr-run" type="button" data-biz="' + biz + '">立即提醒</button></td></tr>';
      }).join("");
      Array.prototype.forEach.call(
        $("rr-tbody").querySelectorAll(".rr-run"), function (btn) {
          btn.addEventListener("click", function () {
            var biz = btn.getAttribute("data-biz");
            GOPS.api(gPath("/reminders/run"), {
              method: "POST", body: { biz: biz }
            }).then(function (r) {
              if (r.status !== 200) {
                GOPS.msg("cfg-rr-msg", r.data.detail || "执行失败", false);
                return;
              }
              var s = r.data.summary || {};
              GOPS.msg("cfg-rr-msg", r.data.message +
                       "（目标 " + (s.targets || 0) + "，新计划 " + (s.new || 0) +
                       "，已发送 " + (s.sent || 0) + "，未发送 " + (s.unsent || 0) + "）",
                       true);
            });
          });
        });
    }

    function save(path, body, msgId) {
      GOPS.api(gPath(path), { method: "PUT", body: body }).then(function (r) {
        GOPS.msg(msgId, r.data.ok ? "已保存" : (r.data.detail || "保存失败"), !!r.data.ok);
      });
    }

    on("cfg-save-basic", "click", function () {
      save("", {
        group_name: $("cfg-group-name").value,
        status: $("cfg-status").value
      }, "cfg-basic-msg");
    });
    on("cfg-save-switches", "click", function () {
      save("/config", { switches: {
        checkin: $("sw-checkin").checked,
        summary: $("sw-summary").checked,
        report: $("sw-report").checked,
        reminder: $("sw-reminder").checked,
        statistics: $("sw-statistics").checked
      } }, "cfg-sw-msg");
    });
    on("cfg-save-deadlines", "click", function () {
      // ⭐ 2026-10-19：只保存总结周期；截止时间字段已废弃（整天执行），
      // ⛔ 不再提交 daily_cutoff（后端仍兼容该字段，但业务不再使用）
      save("/config", {
        period_type: $("cfg-period-type").value
      }, "cfg-dl-msg");
    });
    on("slot-add-btn", "click", function () {
      renderSlotRows(collectSlots().concat(
        [{ id: "", name: "", start: "", end: "" }]));
    });
    on("cfg-save-slots", "click", function () {
      var slots = collectSlots();
      if (!slots.length) {
        GOPS.msg("cfg-slot-msg", "至少配置一个打卡时间段", false);
        return;
      }
      save("/config", { checkin_deadlines: { slots: slots } }, "cfg-slot-msg");
    });
    on("cfg-save-rules", "click", function () {
      var body = { reminder_rules: {} };
      Array.prototype.forEach.call(
        $("rr-tbody").querySelectorAll("tr"), function (tr) {
          var biz = tr.querySelector(".rr-on").getAttribute("data-biz");
          var entry = { enabled: tr.querySelector(".rr-on").checked };
          // 打卡行时间固定（服务端口径）：只送开关，⛔ 不送 times/text
          // （服务端按合并语义保留原值，不会清掉其他业务的规则）
          if (biz !== "checkin") {
            entry.times = tr.querySelector(".rr-times").value.split(/[,，]/)
              .map(function (s) { return s.trim(); })
              .filter(function (s) { return s; });
            entry.text = tr.querySelector(".rr-text").value.trim();
          }
          body.reminder_rules[biz] = entry;
        });
      save("/config", body, "cfg-rr-msg");
    });
    on("cfg-save-image", "click", function () {
      save("/config", { checkin_require_image: $("cfg-require-image").checked },
           "cfg-img-msg");
    });

    loadConfig();
  }

  // ---------- 应交人员 ----------

  function initGroupMembers() {
    function load() {
      GOPS.api(gPath("/members")).then(function (r) {
        if (r.status !== 200) { return; }
        $("mem-tbody").innerHTML = (r.data.members || []).map(function (m) {
          return "<tr><td class=\"mono\">" + GOPS.esc(m.user_id) + "</td><td>" +
            GOPS.esc(m.nickname || "") + "</td><td>" + GOPS.esc(m.role || "") +
            '</td><td><span class="badge ' + (m.enabled ? "bg-success-lt" : "bg-secondary-lt") + '">' +
            (m.enabled ? "启用" : "停用") + "</span></td><td class=\"text-nowrap\">" +
            '<button class="btn btn-sm btn-outline-secondary" data-act="toggle" data-uid="' + GOPS.esc(m.user_id) + '" data-enabled="' + (m.enabled ? "1" : "0") + '">' +
            (m.enabled ? "停用" : "启用") + "</button> " +
            '<button class="btn btn-sm btn-outline-danger" data-act="del" data-uid="' + GOPS.esc(m.user_id) + '">删除</button></td></tr>';
        }).join("") || '<tr><td colspan="5" class="text-muted text-center">暂无应交人员</td></tr>';
        Array.prototype.forEach.call(
          $("mem-tbody").querySelectorAll("button"), function (btn) {
            btn.addEventListener("click", function () {
              var uid = btn.getAttribute("data-uid");
              if (btn.getAttribute("data-act") === "del") {
                if (!window.confirm("删除将停止该人员未来任务（历史数据保留），确认？")) {
                  return;
                }
                GOPS.api(gPath("/members/" + encodeURIComponent(uid)),
                         { method: "DELETE" }).then(reload);
              } else {
                var enabled = btn.getAttribute("data-enabled") === "1";
                GOPS.api(gPath("/members/" + encodeURIComponent(uid)),
                         { method: "PUT", body: { enabled: !enabled } })
                  .then(function (r) {
                    GOPS.msg("mem-msg", r.data.ok ? "已更新" : (r.data.detail || "操作失败"), !!r.data.ok);
                    if (r.data.ok) { load(); }
                  });
              }
            });
          });
      });
    }
    function reload(r) {
      GOPS.msg("mem-msg", r.data.ok ? "已更新" : (r.data.detail || "操作失败"), !!r.data.ok);
      if (r.data.ok) { load(); }
    }
    on("mem-add-btn", "click", function () {
      var uid = $("mem-user").value.trim();
      if (!uid) { GOPS.msg("mem-msg", "请填写用户标识", false); return; }
      GOPS.api(gPath("/members"), {
        method: "POST",
        body: {
          user_id: uid,
          nickname: $("mem-nick").value.trim() || null,
          role: $("mem-role").value.trim() || null
        }
      }).then(function (r) {
        GOPS.msg("mem-msg", r.data.ok ? "已新增" : (r.data.detail || "失败"), !!r.data.ok);
        if (r.data.ok) { $("mem-user").value = ""; load(); }
      });
    });
    load();
  }

  // ---------- 导出白名单 ----------
  // 2026-10-19 用户指令：单权化 + 授权对象限群成员。
  // - 权限只有 ADMIN_ALL 一种（一键 = 全部管理员操作权限），UI 不再选权限
  // - 授权对象从「应交人员」下拉选择（enabled 成员），⛔ 不手输手机号
  // - 已授权成员在下拉里标注「已授权」，避免重复授权

  function initGroupWhitelist() {
    var members = [];
    var granted = {};

    function loadMembers() {
      return GOPS.api(gPath("/members")).then(function (r) {
        if (r.status !== 200) { return; }
        members = (r.data.members || []).filter(function (m) { return m.enabled; });
        var sel = $("wl-user");
        sel.innerHTML = members.map(function (m) {
          var label = (m.nickname || m.user_id) + "（" + m.user_id + "）";
          if (granted[m.user_id]) { label += " · 已授权"; }
          return '<option value="' + GOPS.esc(m.user_id) + '">' +
            GOPS.esc(label) + "</option>";
        }).join("") || '<option value="">（暂无启用的应交人员）</option>';
      });
    }

    function loadList() {
      return GOPS.api(gPath("/whitelist")).then(function (r) {
        if (r.status !== 200) { return; }
        granted = {};
        (r.data.whitelist || []).forEach(function (w) { granted[w.user_id] = true; });
        $("wl-tbody").innerHTML = (r.data.whitelist || []).map(function (w) {
          return "<tr><td class=\"mono\">" + GOPS.esc(w.user_id) + "</td><td>" +
            GOPS.esc(w.nickname || "") + "</td><td>" +
            '<span class="badge bg-primary-lt">' + GOPS.esc(PERM_LABEL[w.permission_type] || w.permission_type) + "</span></td><td>" +
            GOPS.esc(w.updated_at || "") + '</td><td class="text-nowrap">' +
            '<button class="btn btn-sm btn-outline-danger" data-uid="' + GOPS.esc(w.user_id) +
            '">移除</button></td></tr>';
        }).join("") || '<tr><td colspan="5" class="text-muted text-center">暂无授权</td></tr>';
        Array.prototype.forEach.call(
          $("wl-tbody").querySelectorAll("button"), function (btn) {
            btn.addEventListener("click", function () {
              GOPS.api(gPath("/whitelist"), {
                method: "DELETE",
                body: { user_id: btn.getAttribute("data-uid") }
              }).then(function (r) {
                GOPS.msg("wl-msg", r.data.ok ? "已移除" : (r.data.detail || "失败"), !!r.data.ok);
                if (r.data.ok) { loadMembers(); loadList(); }
              });
            });
          });
      });
    }

    on("wl-add-btn", "click", function () {
      var uid = $("wl-user").value;
      if (!uid) { GOPS.msg("wl-msg", "请先选择应交人员", false); return; }
      GOPS.api(gPath("/whitelist"), {
        method: "POST",
        body: { user_id: uid }
      }).then(function (r) {
        GOPS.msg("wl-msg", r.data.ok ? "已授权（全部管理员操作权限）" : (r.data.detail || "失败"), !!r.data.ok);
        if (r.data.ok) { loadMembers(); loadList(); }
      });
    });
    loadMembers();
    loadList();
  }

  // ---------- 报数字段池 ----------
  // 2026-10-16 去角色化（用户指令一）：字段全局共享，⛔ 不再配置/展示「适用角色」；
  // roles 列自此废弃（存量数据保留但不再参与任何过滤）。

  function initGroupFields() {
    function load() {
      GOPS.api(gPath("/fields")).then(function (r) {
        if (r.status !== 200) { return; }
        $("fd-tbody").innerHTML = (r.data.fields || []).map(function (f) {
          return "<tr><td>" + f.field_id + "</td><td>" + GOPS.esc(f.field_name) +
            "</td><td>" + GOPS.esc(f.unit || "") + '</td><td><span class="badge ' +
            (f.enabled ? "bg-success-lt" : "bg-secondary-lt") + '">' +
            (f.enabled ? "启用" : "停用") + "</span></td><td class=\"text-nowrap\">" +
            (f.enabled ? '<button class="btn btn-sm btn-outline-secondary" data-fid="' + f.field_id + '">停用</button>' : "") +
            "</td></tr>";
        }).join("") || '<tr><td colspan="5" class="text-muted text-center">暂无字段</td></tr>';
        Array.prototype.forEach.call(
          $("fd-tbody").querySelectorAll("button"), function (btn) {
            btn.addEventListener("click", function () {
              GOPS.api(gPath("/fields/" + btn.getAttribute("data-fid")),
                       { method: "DELETE" }).then(function (r) {
                GOPS.msg("fd-msg", r.data.ok ? (r.data.message || "已停用") : (r.data.detail || "失败"), !!r.data.ok);
                if (r.data.ok) { load(); }
              });
            });
          });
      });
    }
    on("fd-add-btn", "click", function () {
      var name = $("fd-name").value.trim();
      if (!name) { GOPS.msg("fd-msg", "请填写字段名", false); return; }
      GOPS.api(gPath("/fields"), {
        method: "POST",
        body: { field_name: name, unit: $("fd-unit").value.trim() || null }
      }).then(function (r) {
        GOPS.msg("fd-msg", r.data.ok ? "已新增" : (r.data.detail || "失败"), !!r.data.ok);
        if (r.data.ok) { $("fd-name").value = ""; load(); }
      });
    });
    load();
  }

  // ---------- 后台补录 ----------

  function initGroupBackfill() {
    function loadSlots() {
      GOPS.api(gPath("/config")).then(function (r) {
        if (r.status !== 200) { return; }
        var slots = (r.data.config || {}).checkin_slots || [];
        $("bf-session").innerHTML = slots.map(function (s) {
          return '<option value="' + GOPS.esc(s.id) + '">' +
            GOPS.esc(s.id) + (s.name ? " " + GOPS.esc(s.name) : "") +
            "（" + GOPS.esc(s.start) + "~" + GOPS.esc(s.end) + "）</option>";
        }).join("") || '<option value="">（本群未配置时间段）</option>';
      });
    }

    function loadFields() {
      GOPS.api(gPath("/fields")).then(function (r) {
        if (r.status !== 200) { return; }
        var enabled = (r.data.fields || []).filter(function (f) {
          return f.enabled;
        });
        if (!enabled.length) {
          $("br-fields").innerHTML =
            '<span class="text-muted">本群暂无启用字段，请先在「字段池」配置</span>';
          return;
        }
        $("br-fields").innerHTML = enabled.map(function (f) {
          return '<div class="bf-field"><label class="form-check"><input class="form-check-input" type="checkbox" value="' +
            f.field_id + '"> ' + GOPS.esc(f.field_name) +
            (f.unit ? "（" + GOPS.esc(f.unit) + "）" : "") + '</label>' +
            '<input type="number" min="0" step="1" class="form-control form-control-sm bf-qty" placeholder="数量（非负整数）" disabled></div>';
        }).join("");
        Array.prototype.forEach.call(
          $("br-fields").querySelectorAll('input[type="checkbox"]'),
          function (cb) {
            cb.addEventListener("change", function () {
              cb.parentNode.parentNode.querySelector(".bf-qty").disabled =
                !cb.checked;
            });
          });
      });
    }

    function post(path, body, msgId, okText) {
      GOPS.api(gPath(path), { method: "POST", body: body }).then(function (r) {
        GOPS.msg(msgId, r.data.ok ? okText : (r.data.detail || "补录失败"), !!r.data.ok);
      });
    }

    on("rm-run-btn", "click", function () {
      GOPS.api(gPath("/reminders/run"), { method: "POST", body: {} })
        .then(function (r) {
          if (r.status !== 200) {
            GOPS.msg("rm-msg", r.data.detail || "执行失败", false);
            return;
          }
          var s = r.data.summary || {};
          GOPS.msg("rm-msg", r.data.message + "（目标 " + (s.targets || 0) +
                   "，新计划 " + (s.new || 0) + "，已发送 " + (s.sent || 0) +
                   "，未发送 " + (s.unsent || 0) + "）", true);
        });
    });
    on("bf-submit", "click", function () {
      var user = $("bf-user").value.trim();
      var period = $("bf-period").value.trim();
      if (!user || !period) {
        GOPS.msg("bf-msg", "请填写用户标识与日期", false);
        return;
      }
      post("/backfill/checkin", {
        user_id: user, period_key: period,
        session: $("bf-session").value, status: $("bf-status").value
      }, "bf-msg", "打卡补录成功");
    });
    on("bs-submit", "click", function () {
      var user = $("bs-user").value.trim();
      var period = $("bs-period").value.trim();
      var content = $("bs-content").value.trim();
      if (!user || !period || !content) {
        GOPS.msg("bs-msg", "请填写用户标识、周期与总结内容", false);
        return;
      }
      post("/backfill/summary", {
        user_id: user, period_key: period,
        period_type: $("bs-period-type").value, content: content
      }, "bs-msg", "总结补录成功");
    });
    on("br-submit", "click", function () {
      var user = $("br-user").value.trim();
      var period = $("br-period").value.trim();
      var role = $("br-role").value.trim();
      if (!user || !period || !role) {
        GOPS.msg("br-msg", "请填写用户标识、日期与角色", false);
        return;
      }
      var items = [];
      var invalid = false;
      Array.prototype.forEach.call(
        $("br-fields").querySelectorAll(".bf-field"), function (row) {
          var cb = row.querySelector('input[type="checkbox"]');
          var qty = row.querySelector(".bf-qty");
          if (!cb.checked) { return; }
          var value = qty.value.trim();
          if (value === "" || !/^\d+$/.test(value)) { invalid = true; return; }
          items.push({ field_id: parseInt(cb.value, 10),
                       quantity: parseInt(value, 10) });
        });
      if (invalid) {
        GOPS.msg("br-msg", "勾选字段的数量必须是非负整数", false);
        return;
      }
      if (!items.length) {
        GOPS.msg("br-msg", "请至少勾选一个字段并填数量", false);
        return;
      }
      post("/backfill/report", {
        user_id: user, period_key: period, role: role, items: items
      }, "br-msg", "报数补录成功");
    });

    loadSlots();
    loadFields();
  }

  // ---------- 分派 ----------

  var pages = {
    login: initLogin,
    changepw: initChangepw,
    dashboard: initDashboard,
    groups: initGroups,
    instances: initInstances,
    "group-config": initGroupConfig,
    "group-members": initGroupMembers,
    "group-whitelist": initGroupWhitelist,
    "group-fields": initGroupFields,
    "group-backfill": initGroupBackfill
  };

  document.addEventListener("DOMContentLoaded", function () {
    var page = document.body.getAttribute("data-page");
    if (page && pages[page]) { pages[page](); }
  });
})();
