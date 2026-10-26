/**
 * D46 回归探针：H5 页面「提交成功后必须锁成终态」。
 *
 * 为什么需要它
 * ------------
 * 报数页的 bug 不是「没写 disabled」，而是**写了却被状态重算覆盖**：
 * 成功回调里 submitBtn.disabled = true，但 refreshSubmitState() 每次
 * 改动都会重算 `disabled = !valid || !verified`，用户点一下「验证身份」
 * 或改个数量，按钮就又亮了。这类问题静态 grep 看不出来，必须真跑。
 *
 * 做法
 * ----
 * 用最小 DOM 替身把页面 <script> 跑起来，按真实用户顺序操作：
 *   验证身份 → 添加行/上传图 → 填数量 → 提交 → **再继续交互**
 * 断言提交之后：按钮保持禁用、身份区隐藏、重复点击不再发请求。
 *
 * 用法：node h5_lock_check.js [模板目录]
 * 退出码 0 = 通过，1 = 有断言失败
 */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const TPL = process.argv[2] || path.join(__dirname, "..", "..", "group-ops-bot", "app", "h5templates");

class El {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.options = [];
    this._handlers = {};
    this._attrs = {};
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.disabled = false;
    this.style = {};
    this.files = [];
    this._html = "";
  }
  appendChild(child) {
    this.children.push(child);
    if (child.tagName === "OPTION") { this.options.push(child); }
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter((x) => x !== child);
    return child;
  }
  setAttribute(k, v) { this._attrs[k] = String(v); }
  getAttribute(k) { return this._attrs[k]; }
  addEventListener(type, fn) { (this._handlers[type] = this._handlers[type] || []).push(fn); }
  fire(type, ev) {
    const e = ev || { target: this, preventDefault() {}, stopPropagation() {} };
    (this._handlers[type] || []).slice().forEach((fn) => fn.call(this, e));
  }
  click() { this.fire("click"); }
  querySelectorAll(selector) {
    const want = String(selector).toUpperCase();
    const out = [];
    const walk = (node) => {
      (node.children || []).forEach((c) => {
        if (c.tagName === want) { out.push(c); }
        walk(c);
      });
    };
    walk(this);
    return out;
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) {
    this._html = String(v);
    if (this._html === "") { this.children = []; this.options = []; }
  }
}

function makeDom(ids) {
  const reg = {};
  ids.forEach((pair) => {
    const id = Array.isArray(pair) ? pair[0] : pair;
    const tag = Array.isArray(pair) ? pair[1] : "div";
    const el = new El(tag);
    el.id = id;
    // 对应 HTML 上写死的 disabled 属性（替身不解析 HTML，只能显式声明）
    if (Array.isArray(pair) && pair[2] && pair[2].disabled) { el.disabled = true; }
    reg[id] = el;
  });
  const document = {
    getElementById(id) {
      if (!reg[id]) { reg[id] = new El("div"); }
      return reg[id];
    },
    createElement(tag) { return new El(tag); },
  };
  return { reg, document };
}

function extractScript(file) {
  const html = fs.readFileSync(file, "utf8");
  const m = /<script>([\s\S]*?)<\/script>/.exec(html);
  if (!m) { throw new Error("找不到 <script> 块: " + file); }
  return m[1];
}

function tick(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms || 5));
}

const failures = [];
function check(cond, label) {
  if (!cond) { failures.push(label); }
  console.log((cond ? "  PASS " : "  FAIL ") + label);
}

async function testReport() {
  console.log("[报数 report.html]");
  const js = extractScript(path.join(TPL, "report.html"));
  const { reg, document } = makeDom([
    ["verifyCard", "div"], ["holderName", "span"], ["verifyInput", "input"],
    ["verifyBtn", "button"], ["verifyMsg", "div"], ["fields", "div"],
    ["addBtn", "button", { disabled: true }],
    ["submitBtn", "button", { disabled: true }], ["status", "div"],
  ]);

  let submitCount = 0;
  const fetchStub = (url) => {
    let payload = { ok: false };
    if (url.indexOf("/api/h5/report/info") >= 0) {
      payload = {
        ok: true, done: false, holder_name: "郑佳",
        fields: [{ field_id: 1, field_name: "客户数", unit: "个" }],
      };
    } else if (url.indexOf("/api/h5/report/submit") >= 0) {
      submitCount += 1;
      payload = { ok: true, kind: "A", message: "报数已提交成功" };
    }
    return Promise.resolve({ json: () => Promise.resolve(payload) });
  };

  const sandbox = {
    document, fetch: fetchStub,
    location: { hash: "#t=TOKEN123" },
    setInterval, clearInterval, setTimeout, console,
  };
  vm.createContext(sandbox);
  vm.runInContext(js, sandbox);
  await tick();

  check(reg.submitBtn.disabled === true, "初始化后（未验证身份）提交按钮应禁用");

  reg.verifyInput.value = "0921";
  reg.verifyBtn.fire("click");
  reg.addBtn.fire("click");
  await tick();

  const sel = reg.fields.querySelectorAll("select")[0];
  check(!!sel, "点「添加报数字段」应生成一行下拉");
  sel.value = "1";
  sel.fire("change");
  const qty = reg.fields.querySelectorAll("input")[0];
  qty.value = "3";
  qty.fire("input");
  check(reg.submitBtn.disabled === false, "验证+填好后应可提交");

  reg.submitBtn.fire("click");
  await tick();
  check(submitCount === 1, "提交只发一次请求");
  check(reg.submitBtn.disabled === true, "提交成功后按钮保持禁用");
  check(reg.submitBtn.textContent === "已提交", "按钮文案变为「已提交」");
  check(reg.verifyCard.style.display === "none", "提交后隐藏身份核验区");
  check(reg.fields.innerHTML.indexOf("已提交完成") >= 0, "字段区显示已提交终态");
  check(reg.addBtn.disabled === true, "提交后不能再添加字段");

  reg.verifyInput.value = "0921";
  reg.verifyBtn.fire("click");
  check(reg.submitBtn.disabled === true, "⛔ 提交后重新验证身份不能点亮提交按钮");

  reg.addBtn.fire("click");
  check(reg.submitBtn.disabled === true, "⛔ 提交后再点「添加」不能点亮提交按钮");

  reg.submitBtn.fire("click");
  await tick();
  check(submitCount === 1, "⛔ 提交后再点提交不再发请求");
}

async function testCheckin() {
  console.log("[打卡 checkin.html]");
  const js = extractScript(path.join(TPL, "checkin.html"));
  const { reg, document } = makeDom([
    ["session", "span"], ["period", "span"], ["state", "span"],
    ["verifyCard", "div"], ["holderName", "span"], ["verifyInput", "input"],
    ["verifyBtn", "button"], ["verifyMsg", "div"],
    ["uploadCard", "div"], ["cameraBtn", "button"], ["galleryBtn", "button"],
    ["fileCamera", "input"], ["fileGallery", "input"], ["fileName", "div"],
    ["uploadHint", "div"], ["expireHint", "div"],
    ["submitBtn", "button", { disabled: true }], ["status", "div"],
  ]);

  let submitCount = 0;
  let uploadCount = 0;
  const fetchStub = (url) => {
    let payload = { ok: false };
    if (url.indexOf("/api/h5/checkin/info") >= 0) {
      payload = {
        ok: true, done: false, session: "pm", session_label: "下午",
        period_key: "2026-10-26", holder_name: "郑佳",
      };
    } else if (url.indexOf("/api/h5/checkin/upload") >= 0) {
      uploadCount += 1;
      payload = { ok: true, image_ref: "ref-" + uploadCount };
    } else if (url.indexOf("/api/h5/checkin/submit") >= 0) {
      submitCount += 1;
      payload = { ok: true, kind: "A", message: "打卡已提交成功" };
    }
    return Promise.resolve({ json: () => Promise.resolve(payload) });
  };

  const sandbox = {
    document, fetch: fetchStub,
    location: { hash: "#t=TOKEN456" },
    setInterval, clearInterval, setTimeout, console,
    FormData, Blob,
  };
  vm.createContext(sandbox);
  vm.runInContext(js, sandbox);
  await tick();

  check(reg.cameraBtn.disabled === true, "初始化后未验证身份不许上传");
  check(reg.submitBtn.disabled === true, "初始化后提交按钮应禁用");

  reg.verifyInput.value = "0921";
  reg.verifyBtn.fire("click");
  check(reg.cameraBtn.disabled === false, "验证后可上传");

  reg.fileCamera.files = [new Blob(["x"], { type: "image/png" })];
  reg.fileCamera.fire("change");
  await tick();
  check(reg.submitBtn.disabled === false, "上传成功后应可提交");

  reg.submitBtn.fire("click");
  await tick();
  check(submitCount === 1, "提交只发一次请求");
  check(reg.submitBtn.textContent === "已提交", "按钮文案变为「已提交」");
  check(reg.submitBtn.disabled === true, "提交成功后按钮保持禁用");
  check(reg.verifyCard.style.display === "none", "提交后隐藏身份核验区");
  check(reg.uploadCard.style.display === "none", "提交后隐藏上传区");
  check(reg.state.textContent === "已完成", "状态显示已完成");

  reg.verifyInput.value = "0921";
  reg.verifyBtn.fire("click");
  check(reg.submitBtn.disabled === true, "⛔ 提交后重新验证不能点亮提交");

  reg.fileCamera.files = [new Blob(["y"], { type: "image/png" })];
  reg.fileCamera.fire("change");
  await tick();
  check(reg.submitBtn.disabled === true, "⛔ 提交后再上传图片不能点亮提交");

  reg.submitBtn.fire("click");
  await tick();
  check(submitCount === 1, "⛔ 提交后再点提交不再发请求");
}

(async () => {
  if (!fs.existsSync(path.join(TPL, "report.html"))) {
    console.log("找不到模板目录: " + TPL);
    process.exit(2);
  }
  await testReport();
  await testCheckin();
  console.log("");
  if (failures.length) {
    console.log("失败 " + failures.length + " 项：");
    failures.forEach((f) => console.log("  - " + f));
    process.exit(1);
  }
  console.log("全部通过");
  process.exit(0);
})().catch((e) => {
  console.log("探针异常: " + (e && e.stack || e));
  process.exit(1);
});
