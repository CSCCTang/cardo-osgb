/* CardoViewer · 项目包加载器
 * 职责：读 viewer/default-project.txt（或 ?project= 指定）→ 读项目包 project.json
 *      → 按 viewer/project.schema.json 校验 → 返回可用的配置与路径解析器。
 * 原则：缺必填字段、类型不符、文件不存在时直接停页并给出字段路径，不静默兜底成空白。
 * 同一套规则在 scripts/project_pack.py 里有等价实现，供 开工.py 在起服务前先拦一次。
 */
(function (global) {
  "use strict";

  var ROOT = new URL("../../", location.href);
  var SCHEMA_URL = new URL("viewer/project.schema.json", ROOT);

  var TYPES = {
    string: function (v) { return typeof v === "string" && v.trim() !== ""; },
    number: function (v) { return typeof v === "number" && isFinite(v); },
    integer: function (v) { return typeof v === "number" && isFinite(v) && Math.floor(v) === v; },
    boolean: function (v) { return typeof v === "boolean"; },
    array: function (v) { return Array.isArray(v); },
    object: function (v) { return v !== null && typeof v === "object" && !Array.isArray(v); }
  };

  function describe(v) {
    if (v === undefined) return "（未提供）";
    if (v === null) return "null";
    if (Array.isArray(v)) return "数组（" + v.length + " 项）";
    if (typeof v === "object") return "对象";
    if (typeof v === "string") return v === "" ? "空字符串" : "字符串「" + v + "」";
    return typeof v + " " + String(v);
  }

  /* 把 "layers[].file" 这类路径展开成 [项目包里的路径, 值] 列表 */
  function walk(node, tokens, prefix, out) {
    var tok = tokens[0], rest = tokens.slice(1);
    var isArr = tok.slice(-2) === "[]";
    var key = isArr ? tok.slice(0, -2) : tok;
    var path = (prefix ? prefix + "." : "") + key;
    if (!node || typeof node !== "object") {
      if (!rest.length) out.push({ parent: node, key: key, value: undefined, path: path });
      return;
    }
    if (isArr) {
      var arr = node[key];
      if (!Array.isArray(arr)) {
        if (!rest.length) out.push({ parent: node, key: key, value: arr, path: path, missing: arr === undefined });
        return;
      }
      arr.forEach(function (item, i) {
        if (!rest.length) out.push({ parent: arr, key: i, value: item, path: path + "[" + i + "]" });
        else walk(item, rest, path + "[" + i + "]", out);
      });
      return;
    }
    var v = node[key];
    if (!rest.length) out.push({ parent: node, key: key, value: v, path: path, missing: v === undefined });
    else walk(v, rest, path, out);
  }

  function getAt(node, dotted) {
    var parts = dotted.split(".");
    for (var i = 0; i < parts.length; i++) {
      if (!node || typeof node !== "object") return undefined;
      node = node[parts[i]];
    }
    return node;
  }

  function isEmpty(v) {
    return v === undefined || v === null || (typeof v === "string" && v.trim() === "");
  }

  function validate(cfg, rules) {
    var problems = [];
    rules.forEach(function (rule) {
      var entries = [];
      walk(cfg, rule.path.split("."), "", entries);
      entries.forEach(function (e) {
        var required = rule.required !== false;
        if (rule.requiredWhenPresent) {
          required = required && !isEmpty(getAt(cfg, rule.requiredWhenPresent));
        }
        if (rule.requireIfSame) {
          var same = (e.parent && typeof e.parent === "object") ? e.parent[rule.requireIfSame.path] : undefined;
          required = same === rule.requireIfSame.equals;
        }
        var expect = rule.type + (rule.enum ? "，取值 " + rule.enum.join(" / ") : "");
        if (rule.length) expect += "，长度 " + rule.length;
        if (rule.minItems) expect += "，至少 " + rule.minItems + " 项";

        if (isEmpty(e.value)) {
          if (required) {
            problems.push({ path: e.path, kind: "缺少必填字段", expect: expect, actual: describe(e.value) });
          }
          return;
        }
        if (rule.type && !TYPES[rule.type](e.value)) {
          problems.push({ path: e.path, kind: "类型不符", expect: expect, actual: describe(e.value) });
          return;
        }
        if (rule.enum && rule.enum.indexOf(e.value) < 0) {
          problems.push({ path: e.path, kind: "取值不在允许范围", expect: expect, actual: describe(e.value) });
          return;
        }
        if (rule.length && e.value.length !== rule.length) {
          problems.push({ path: e.path, kind: "长度不符", expect: expect, actual: describe(e.value) });
          return;
        }
        if (rule.minItems && e.value.length < rule.minItems) {
          problems.push({ path: e.path, kind: "条目不足", expect: expect, actual: describe(e.value) });
          return;
        }
        if (rule.items && Array.isArray(e.value)) {
          for (var i = 0; i < e.value.length; i++) {
            if (!TYPES[rule.items](e.value[i])) {
              problems.push({ path: e.path + "[" + i + "]", kind: "元素类型不符", expect: rule.items, actual: describe(e.value[i]) });
              return;
            }
          }
        }
      });
    });
    /* 上级整块缺失时，不再重复报它的子字段 */
    var kept = [];
    problems.forEach(function (p) {
      var covered = kept.some(function (k) {
        return k.kind === "缺少必填字段" && p.path.indexOf(k.path + ".") === 0;
      });
      if (!covered) kept.push(p);
    });
    return kept;
  }

  function encode(rel) {
    return String(rel).split("/").filter(Boolean).map(function (s) {
      return s === ".." || s === "." ? s : encodeURIComponent(s);
    }).join("/");
  }

  function fatal(message, problems, configUrl) {
    var err = new Error(message);
    err.problems = problems || [];
    err.configUrl = configUrl || null;
    err.cardo = true;
    return err;
  }

  async function fetchJson(url, label) {
    var resp;
    try {
      resp = await fetch(url, { cache: "no-store" });
    } catch (e) {
      throw fatal(label + " 读取失败：" + e.message, [], url);
    }
    if (!resp.ok) {
      throw fatal(label + " 读取失败：HTTP " + resp.status + "（" + decodeURI(url.href || url) + "）", [], url);
    }
    var text = await resp.text();
    try {
      return JSON.parse(text);
    } catch (e) {
      throw fatal(label + " 不是合法 JSON：" + e.message, [], url);
    }
  }

  async function resolveProjectPath() {
    var q = new URLSearchParams(location.search).get("project");
    if (q && q.trim()) return q.trim().replace(/^\.?\/+/, "").replace(/\/+$/, "");
    var url = new URL("viewer/default-project.txt", ROOT);
    var resp;
    try {
      resp = await fetch(url, { cache: "no-store" });
    } catch (e) {
      throw fatal("默认项目包未配置：viewer/default-project.txt 读取失败（" + e.message + "）");
    }
    if (!resp.ok) {
      throw fatal("默认项目包未配置：viewer/default-project.txt 读取失败（HTTP " + resp.status + "）");
    }
    var text = (await resp.text()).trim();
    if (!text) throw fatal("默认项目包未配置：viewer/default-project.txt 为空");
    return text.replace(/^\.?\/+/, "").replace(/\/+$/, "");
  }

  async function load() {
    var projectPath = await resolveProjectPath();
    var dir = new URL(encode(projectPath) + "/", ROOT);
    var configUrl = new URL("project.json", dir);
    var cfg = await fetchJson(configUrl, "项目包配置 project.json ");
    var schema = await fetchJson(SCHEMA_URL, "契约文件 viewer/project.schema.json ");
    var problems = validate(cfg, schema.rules || []);
    if (problems.length) {
      throw fatal("项目包配置有 " + problems.length + " 处不满足契约", problems, configUrl);
    }
    return {
      config: cfg,
      dir: dir,
      url: configUrl,
      path: projectPath,
      resolve: function (rel) {
        return new URL(encode(rel), dir).href;
      }
    };
  }

  function showError(err) {
    var rows = (err && err.problems) || [];
    var box = document.createElement("div");
    box.id = "cardo-fatal";
    box.setAttribute("style", "position:fixed;inset:0;z-index:99999;overflow:auto;background:#1b1113;" +
      "color:#f3e9e9;font:13px/1.7 'Microsoft YaHei',system-ui,sans-serif;padding:40px 32px;");

    var h = document.createElement("div");
    h.textContent = "项目包加载失败";
    h.setAttribute("style", "font-size:20px;font-weight:700;color:#ff8a8a;margin-bottom:6px;");
    box.appendChild(h);

    var sub = document.createElement("div");
    sub.textContent = (err && err.message) || "未知错误";
    sub.setAttribute("style", "margin-bottom:18px;color:#e6c9c9;");
    box.appendChild(sub);

    if (err && err.configUrl) {
      var cfgLine = document.createElement("div");
      cfgLine.textContent = "配置文件：" + decodeURI(err.configUrl.href || String(err.configUrl));
      cfgLine.setAttribute("style", "margin-bottom:14px;color:#c9b3b3;word-break:break-all;");
      box.appendChild(cfgLine);
    }

    rows.forEach(function (p, i) {
      var card = document.createElement("div");
      card.setAttribute("style", "max-width:860px;margin:0 0 10px;padding:10px 14px;border:1px solid #6b2b2b;" +
        "border-left:3px solid #ff6b6b;border-radius:4px;background:#251719;");
      [["字段", "project.json → " + p.path],
       ["问题", p.kind],
       ["期望", p.expect],
       ["实际", p.actual]].forEach(function (kv) {
        var line = document.createElement("div");
        var b = document.createElement("b");
        b.textContent = kv[0] + "：";
        b.setAttribute("style", "color:#ffb3b3;font-weight:600;");
        line.appendChild(b);
        line.appendChild(document.createTextNode(kv[1] === undefined || kv[1] === "" ? "—" : String(kv[1])));
        card.appendChild(line);
      });
      box.appendChild(card);
    });

    var tip = document.createElement("div");
    tip.setAttribute("style", "max-width:860px;margin-top:18px;padding-top:14px;border-top:1px solid #4a2a2c;color:#c9b3b3;");
    tip.innerHTML = "字段说明见 <b style=\"color:#ffb3b3\">projects/README.md</b>；" +
      "「最小可用集」一节列出能跑起来的最少字段。<br>修正 project.json 后刷新本页即可，不需要改查看器代码。";
    box.appendChild(tip);

    document.body.appendChild(box);
    if (global.console) console.error("[CardoViewer] 项目包加载失败", err);
  }

  global.CardoProject = {
    load: load,
    validate: validate,
    showError: showError,
    root: ROOT
  };
})(window);
