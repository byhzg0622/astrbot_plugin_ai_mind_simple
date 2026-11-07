/**
 * 面板前端的沙箱测试。
 *
 * 为什么需要它：面板跑在不透明源的 iframe 里，自己发 fetch 会被判跨域失败，
 * 只能走宿主注入的 postMessage 桥。但桥接受的端点前缀在不同 AstrBot 版本里不一致，
 * 所以页面里做了"逐个候选探测"。这段逻辑没法用 Python 测，只能用 Node 把它跑起来。
 *
 *     node tests/panel_check.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const HTML = readFileSync(join(HERE, "..", "pages", "mind", "index.html"), "utf8");
const BLOCKS = [...HTML.matchAll(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (BLOCKS.length === 0) throw new Error("页面里一个内联 script 都没有");
// 页面里除了主逻辑，还可能有一小段引导脚本（例如「尽早贴上主题、避免白闪」——
// 那段必须在 <head> 里同步跑，放到文末就来不及了）。
// 真正要分析的是最长的那段，也就是主逻辑。
const SCRIPT = BLOCKS.reduce((a, b) => (b.length > a.length ? b : a));

//: 情绪原型全表 —— 唯一真源是 mind/emotion/model.py 的 PROTOTYPES。
//: 面板「一键改成」的按钮必须与它一一对应：分类是「离哪个锚点近就是哪种情绪」，
//: 锚点写错一位，点「亲昵」就会变成「感动」（2026-09 就是这么坏的）。
const MODEL_SRC = readFileSync(join(HERE, "..", "mind", "emotion", "model.py"), "utf8");
const PROTO_TYPES = [...MODEL_SRC.matchAll(
  /Prototype\("([^"]+)", "([^"]+)", "([^"]+)", \((-?[\d.]+),\s*(-?[\d.]+),\s*(-?[\d.]+)\)/g
)].map((m) => ({
  key: m[1], name: m[2], emoji: m[3],
  p: Number(m[4]), a: Number(m[5]), d: Number(m[6]),
}));
if (PROTO_TYPES.length < 20) {
  console.log("❌ 从 model.py 只解析出 " + PROTO_TYPES.length + " 个情绪原型，正则失效了");
  process.exit(1);
}

// 静态检查：JS 的函数声明是"后者覆盖前者"，
// 一旦文件里出现重复定义（例如补丁把一段插了两次），跑起来的可能根本不是新代码。
// 这个坑真踩过一次，所以固化成检查项。
{
  const names = [...SCRIPT.matchAll(/^  function (\w+)\(/gm)].map((m) => m[1]);
  const dup = [...new Set(names.filter((n, i) => names.indexOf(n) !== i))];
  if (dup.length) {
    console.log("❌ 面板脚本里有重复的函数定义：" + dup.join(", "));
    process.exit(1);
  }
  console.log("✅ 面板脚本无重复函数定义（共 " + names.length + " 个函数）");
}

// ---- 静态检查：id 不能重复 ----
// 真踩过：重建页面区域时把旧的保存卡片留下了，页面上出现两个 id="pr-save"，
// 于是 $("pr-save") 拿到的是先出现的那个 —— 按钮点了没反应，还很难查。
{
  const ids = [...HTML.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]);
  const dup = [...new Set(ids.filter((id, i) => ids.indexOf(id) !== i))];
  if (dup.length) {
    console.log("❌ 面板里有重复的 id：" + dup.join(", "));
    process.exit(1);
  }
  console.log("✅ 面板 id 无重复（共 " + ids.length + " 个）");
}

// ---- 结构检查：视图不能被套在另一个视图里 ----
// 真踩过：新加的「分段拟人」视图插在了「人格定制」的收尾 </div> 之前，
// 于是它成了 view-persona 的子节点 —— 点页签时自己的 display 是打开了，
// 但父节点是 display:none，整页看上去就是一片空白。
// 标签配平检查抓不到这种错，必须单独卡一道。
{
  const cleaned = HTML
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "");
  const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input",
                        "link", "meta", "param", "source", "track", "wbr"]);
  const re = /<(\/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*?)(\/?)>/g;
  const stack = [];
  const problems = [];
  const views = [];
  let match;
  while ((match = re.exec(cleaned)) !== null) {
    const closing = match[1] === "/";
    const tag = match[2].toLowerCase();
    if (VOID.has(tag) || match[4] === "/") continue;
    if (!closing) {
      const found = /\sid="(view-[a-z]+)"/.exec(match[3]);
      const view = found ? found[1] : "";
      if (view) {
        views.push(view);
        // 正确的位置是 <body><div class="wrap"><div id="view-*">，也就是栈里正好三层
        if (stack.length !== 3) {
          problems.push(view + " 被套在别的元素里（当前打开的标签：" + stack.join(" > ") + "）");
        }
      }
      stack.push(tag);
      continue;
    }
    stack.pop();
  }
  if (problems.length) {
    console.log("❌ 面板视图被套住了，点页签会显示空白：");
    problems.forEach((p) => console.log("   " + p));
    process.exit(1);
  }
  console.log("✅ 面板视图都是 .wrap 的直接子节点（" + views.length + " 个：" + views.join(" ") + "）");
}

// ---- 静态检查：不许用会被沙箱拦掉的模态框 ----
// 沙箱 iframe 默认没有 allow-modals，window.confirm() 会被浏览器直接拦掉并返回 false，
// 代码就停在 if (!confirm(...)) return; 那一行 —— 表现为"点了没反应也不报错"。
// 面板里全部破坏性操作都踩过这个坑，所以固化成一票否决。
{
  const modalCalls = [...SCRIPT.matchAll(/window\.(confirm|alert|prompt)\s*\(/g)];
  if (modalCalls.length) {
    console.log("❌ 面板脚本里还有 " + modalCalls.length + " 处 window.confirm/alert/prompt");
    console.log("   沙箱 iframe 会静默拦掉它们，请改用 needConfirm() 两步确认");
    process.exit(1);
  }
  console.log("✅ 面板脚本没有用会被沙箱拦截的模态框");
}

// ---- 静态检查：「调节心境」的排版 ----
// 报过「字错位」，两个原因：
//   ① 提示挤在标题右边（max-width:48% + 右对齐），卡片一窄 .row 就换行，
//      提示块被顶到下一行却还保持右对齐 —— 第二行「基准附近」看着就是错开的；
//   ② 中文默认能在任意字之间断行，说明文案被切成了「…窗口内持 / 续推进」。
// 所以固化成两条：提示独占一行 + 中文只按词断行。
{
  const problems = [];
  const start = HTML.indexOf('id="mood-ops-hint"');
  if (start < 0) {
    problems.push("找不到 id=mood-ops-hint");
  } else {
    const tag = HTML.slice(HTML.lastIndexOf("<div", start), HTML.indexOf(">", start) + 1);
    if (/max-width/.test(tag)) problems.push("mood-ops-hint 还带着 max-width（会跟标题挤成两行）");
    if (/text-align\s*:\s*right/.test(tag)) problems.push("mood-ops-hint 还是右对齐（换行后第二行会错位）");
    if (!/word-break\s*:\s*keep-all/.test(tag)) problems.push("mood-ops-hint 没有 word-break:keep-all（中文会在字中间断行）");
  }
  const sub = HTML.indexOf("心境 = 基线 + 下面三个");
  if (sub < 0) {
    problems.push("找不到心境卡的说明文案");
  } else {
    const tag = HTML.slice(HTML.lastIndexOf("<div", sub), HTML.indexOf(">", sub) + 1);
    if (!/word-break\s*:\s*keep-all/.test(tag)) {
      problems.push("心境说明没加 word-break:keep-all（会被切成「窗口内持 / 续推进」）");
    }
  }
  if (problems.length) {
    console.log("❌ 「调节心境」卡片的排版退回了：");
    problems.forEach((p) => console.log("   " + p));
    process.exit(1);
  }
  console.log("✅ 「调节心境」提示独占一行、中文只按词断行");
}

// ---- 静态检查：会话下拉要给「未激活」的会话打标记 ----
// 备份搬过来的会话在新机器上还没聊过，而会话下拉跟下面每一张卡绑定 ——
// 看不见就等于「记忆丢了」。标记必须在，且只认 active === false
//（老数据缺这个字段时不该乱标）。
{
  const problems = [];
  if (SCRIPT.indexOf("（未激活）") < 0) {
    problems.push("sessionOption 里没有「（未激活）」标记");
  }
  if (SCRIPT.indexOf("s.active === false") < 0) {
    problems.push("没有按 active === false 判定");
  }
  if (problems.length) {
    console.log("❌ 会话下拉的「未激活」标记退回了：");
    problems.forEach((p) => console.log("   " + p));
    process.exit(1);
  }
  console.log("✅ 会话下拉会给未激活的会话打「（未激活）」标记");
}

// ---- 静态检查：「刷新」必须在搜索框右边（同一个 nowrap 容器）----
// 报过的：卡片一窄，.top 就换行，「刷新」被一个人丢到下一行。
// 三件套捆在一个 nowrap 容器里：要么一起待在第一行，要么整组换行 —— 位置关系不变。
{
  const problems = [];
  const start = HTML.indexOf('id="top-controls"');
  if (start < 0) {
    problems.push("找不到 #top-controls（下拉 / 搜索 / 刷新 的容器）");
  } else {
    const openTag = HTML.slice(
      HTML.lastIndexOf("<div", start), HTML.indexOf(">", start) + 1
    );
    if (!/flex-wrap\s*:\s*nowrap/.test(openTag)) {
      problems.push("#top-controls 不是 nowrap（三件套会被拆开、刷新掉到下一行）");
    }
    const body = HTML.slice(start, HTML.indexOf("</div>", start));
    const order = ['id="session"', 'id="session-search"', 'id="refresh"'];
    let last = -1;
    for (const id of order) {
      const at = body.indexOf(id);
      if (at < 0) {
        problems.push(id + " 不在 #top-controls 里");
        continue;
      }
      if (last >= 0 && at < last) {
        problems.push(id + " 位置不对（要按 下拉 → 搜索 → 刷新 排）");
      }
      last = at;
    }
  }
  if (problems.length) {
    console.log("❌ 顶栏排版退回了：");
    problems.forEach((p) => console.log("   " + p));
    process.exit(1);
  }
  console.log("✅ 「刷新」在搜索框右边（同一个 nowrap 容器，一窄整组换行）");
}

// ---- 结构检查：标签必须配平 ----
// 真踩过：插入新按钮时多留了一个没闭合的 <div class="row">，
// .row 是 flex 容器，后面所有按钮行被并进同一行挤成一团，看起来像"按钮消失了"。
// Python 侧完全看不见这种问题，所以在这里卡住。
function checkHtmlBalance(html) {
  const cleaned = html
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "");
  const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input",
                        "link", "meta", "param", "source", "track", "wbr"]);
  const stack = [];
  const problems = [];
  const re = /<(\/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*?)(\/?)>/g;
  let match;
  while ((match = re.exec(cleaned)) !== null) {
    const closing = match[1] === "/";
    const tag = match[2].toLowerCase();
    if (VOID.has(tag) || match[4] === "/") continue;
    if (!closing) {
      stack.push(tag);
      continue;
    }
    const top = stack.pop();
    if (top !== tag) {
      problems.push("闭合标签 </" + tag + "> 对不上，此时栈顶是 " + (top || "（空）"));
      break;
    }
  }
  if (!problems.length && stack.length) {
    problems.push("有 " + stack.length + " 个标签没闭合：" + stack.join(" > "));
  }
  return problems;
}

{
  const problems = checkHtmlBalance(HTML);
  if (problems.length) {
    console.log("❌ 面板 HTML 结构有问题：");
    problems.forEach((p) => console.log("   " + p));
    process.exit(1);
  }
  console.log("✅ 面板 HTML 标签配平");
}

// ---- 结构检查：管理卡片必须待在情绪曲线页里 ----
// 真踩过：把它们插在 view-emotion 的闭合 </div> **之后**，于是它们跑到了视图外面，
// 点哪个页签都跟着一起显示，看着就像"放到了别的页里"。
{
  const cleaned = HTML
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "");
  const grab = (id) => {
    const start = cleaned.indexOf('<div id="' + id + '"');
    if (start < 0) return "";
    let depth = 0;
    for (let i = start; i < cleaned.length - 4; i++) {
      if (cleaned.startsWith("<div", i)) depth++;
      else if (cleaned.startsWith("</div>", i)) {
        depth--;
        if (depth === 0) return cleaned.slice(start, i + 6);
      }
    }
    return "";
  };
  const emotion = grab("view-emotion");
  const must = ["管理与维护", "数据维护", "记忆清理", "隐私与数据"];
  const missing = must.filter((k) => emotion.indexOf(k) < 0);
  if (!emotion || missing.length) {
    console.log("❌ 情绪曲线页里缺这些卡片：" + (missing.join("、") || "整页都抓不到"));
    process.exit(1);
  }
  const others = ["view-memory", "view-graph", "view-settings"]
    .filter((id) => must.some((k) => grab(id).indexOf(k) >= 0));
  if (others.length) {
    console.log("❌ 这些管理卡片跑到了别的页里：" + others.join("、"));
    process.exit(1);
  }
  // 这两块是**不该**有的：
  // 「和人的关系」随好感度一起删掉；下方那张「情绪」卡片和右列「操纵情绪」完全重复
  const banned = ["和人的关系", "情绪（作用在顶部选中的会话）", 'id="users"', 'id="mg-set"'];
  const leaked = banned.filter((k) => HTML.indexOf(k) >= 0);
  if (leaked.length) {
    console.log("❌ 该删的卡片/控件还在：" + leaked.join("、"));
    process.exit(1);
  }
  console.log("✅ 管理卡片都在情绪曲线页里（" + must.length + " 张），重复与好感度卡片已清");
}

// ---- 静态检查：已删功能的控件不许复活 ----
// 作用域下拉、「关于：」下拉、常驻按钮 —— 这些是用户明确要求去掉的。
{
  const removed = ["mscope", "maddpin", "mbulk-pin", "mbulk-unpin", "maddwho",
                   "gp-pin", "gp-unpin", "gp-scope", "gp-newscope", "gp-applyscope",
                   "mbulk-scope", "tab-theory", "tab-manage", "tab-wizard", "bg-bar"];
  const back = removed.filter((id) => HTML.indexOf('id="' + id + '"') >= 0);
  if (back.length) {
    console.log("❌ 已删掉的控件又回来了：" + back.join(", "));
    process.exit(1);
  }
  const stray = ["关于：", "改作用域", "全部作用域"].filter((s) => HTML.indexOf(s) >= 0);
  if (stray.length) {
    console.log("❌ 页面里还有已删掉的选项文案：" + stray.join("、"));
    process.exit(1);
  }
  if (HTML.indexOf('value="pinned"') >= 0) {
    console.log("❌ 筛选里还有「仅常驻」—— 常驻这个概念已经从面板上拿掉了");
    process.exit(1);
  }
  console.log("✅ 作用域/常驻等已删控件没有复活");
}

// ---- 静态检查：手动添加的类型下拉必须含「关系」 ----
// 真踩过：搜索栏的类型下拉有 5 种，添加栏只有 4 种，少了 relation。
// 上一版只在「搜索栏」里有，加记忆时加不出关系类型。
{
  const m = /<select id="maddkind">([\s\S]*?)<\/select>/.exec(HTML);
  if (!m) {
    console.log("❌ 找不到手动添加的类型下拉 #maddkind");
    process.exit(1);
  }
  const kinds = [...m[1].matchAll(/value="([a-z]+)"/g)].map((x) => x[1]);
  const need = ["fact", "preference", "event", "promise", "relation"];
  const missing = need.filter((k) => kinds.indexOf(k) < 0);
  if (missing.length) {
    console.log("❌ 手动添加的类型下拉缺了：" + missing.join("、"));
    process.exit(1);
  }
  if (kinds.length !== need.length) {
    console.log("❌ 手动添加的类型下拉有重复或多出的项：" + kinds.join(","));
    process.exit(1);
  }
  console.log("✅ 手动添加支持 5 种类型（含「关系」）");
}

// ---- 静态检查：图谱画布必须有交互绑定 ----
// 真踩过：重写 bind() 时把画布的 pointer/wheel 监听整个漏了 ——
// 图看着能画出来，但拖不动、点不中、鼠标放上去也不出提示。
{
  const need = ['addEventListener("pointerdown"', 'addEventListener("pointermove"',
                'addEventListener("pointerup"', 'addEventListener("wheel"'];
  const missing = need.filter((s) => SCRIPT.indexOf(s) < 0);
  if (missing.length) {
    console.log("❌ 图谱画布缺了这些交互绑定：" + missing.join(" "));
    console.log("   后果：拖不动节点、点不中、没有悬停提示、缩放失效");
    process.exit(1);
  }
  if (SCRIPT.indexOf("function gpBindCanvas()") < 0) {
    console.log("❌ 找不到 gpBindCanvas()");
    process.exit(1);
  }
  console.log("✅ 图谱画布有拖动/缩放/悬停/点选绑定");
}

// ---- 静态检查：记忆来源标签要能说出具体来处 ----
{
  if (SCRIPT.indexOf("function sourceLabel(") < 0 || SCRIPT.indexOf("sourceLabel(m)") < 0) {
    console.log("❌ 记忆列表还在用笼统的作用域文案，没有 sourceLabel()");
    process.exit(1);
  }
  if (SCRIPT.indexOf("SCOPE_LABEL") >= 0) {
    console.log("❌ SCOPE_LABEL（「该用户」那种写法）还没清掉");
    process.exit(1);
  }
  console.log("✅ 记忆来源标签会显示具体的人/会话");
}

// ---- 静态检查：本轮改造的东西都要在 / 该走的都走了 ----
{
  const needIds = ["msrc", "maddglobal", "mbulk-global", "mbulk-session"];
  const missing = needIds.filter((id) => HTML.indexOf('id="' + id + '"') < 0);
  if (missing.length) {
    console.log("❌ 面板缺控件：" + missing.join(", "));
    process.exit(1);
  }
  // 这些是本轮按反馈明确要求**删掉**的，回来一个都不行
  const goneIds = ["gp-people", "mowner", "theme-btn"];
  const back = goneIds.filter((id) => HTML.indexOf('id="' + id + '"') >= 0);
  if (back.length) {
    console.log("❌ 已删掉的控件又回来了：" + back.join(", "));
    process.exit(1);
  }
  const problems = [];
  if (SCRIPT.indexOf('class="chip pin">常驻') >= 0) {
    problems.push("列表里还挂着「常驻」标签");
  }
  if (SCRIPT.indexOf("state.gpSelect") >= 0 || SCRIPT.indexOf("state.gpSelected") >= 0) {
    problems.push("图谱按钮还在用写错的字段名 gpSelect/gpSelected");
  }
  if (SCRIPT.indexOf("state.graphSelect = !state.graphSelect") < 0) {
    problems.push("「多选管理」按钮没有切换 graphSelect");
  }
  if (SCRIPT.indexOf("query.source = src") < 0) {
    problems.push("「手动/自动」筛选没传给后端");
  }
  if (SCRIPT.indexOf("query.owner = owner") >= 0) {
    problems.push("人员筛选已删掉，但 JS 还在传 owner");
  }
  if (SCRIPT.indexOf("rebuildOwners") >= 0 || SCRIPT.indexOf("data.owners") >= 0) {
    problems.push("rebuildOwners / owners 还残留（人员下拉已删）");
  }
  if (SCRIPT.indexOf('body.scope = "global"') < 0) {
    problems.push("手动添加的「通用」勾选没生效");
  }
  if (SCRIPT.indexOf('bulkAction("scope", { scope: "global" })') < 0) {
    problems.push("没有「设为通用」批量按钮的处理");
  }
  if (SCRIPT.indexOf('bulkAction("scope", { scope: "session", session: state.session })') < 0) {
    problems.push("没有「设为当前会话」批量按钮的处理");
  }

  // --- 列表勾选：复选框必须能写回 state.selected ---
  if (SCRIPT.indexOf("function syncPageSelection()") < 0) {
    problems.push("没有 syncPageSelection —— 复选框点得动但 selectedIds() 是空的");
  }
  if (SCRIPT.indexOf('$("mlist").onchange') < 0) {
    problems.push("列表复选框没有绑定 change 事件");
  }

  // --- 图谱「点人方块连选」按反馈删掉；全选/清空仍然要能用 ---
  if (SCRIPT.indexOf("gpSelectPerson") >= 0) {
    problems.push("gpSelectPerson 还在（已按反馈删除：列表+全选能覆盖同样诉求）");
  }
  if (SCRIPT.indexOf('bulkAction("delete"') >= 0 && SCRIPT.indexOf("function bulkAction") < 0) {
    problems.push("bulkAction 被引用但没定义");
  }

  // --- 通用配置：分组折叠绑定了吗？「恢复默认」有确认、有反馈吗？ ---
  if (SCRIPT.indexOf('$("st-groups").onclick') < 0) {
    problems.push("分组的「折叠」按钮没绑事件 —— 点了完全没反应");
  }
  if (HTML.indexOf('id="st-reload">恢复默认</button>') < 0) {
    problems.push("「恢复默认」按钮不在（它取代了原来的「重新读取」）");
  }
  if (SCRIPT.indexOf('request("settings/reset"') < 0) {
    problems.push("恢复默认没调 settings/reset —— 点了不会真的还原配置");
  }
  const reload = /\$\("st-reload"\)\.onclick = function/.exec(SCRIPT);
  if (!reload) {
    problems.push("「恢复默认」没有处理函数");
  } else {
    const tail = SCRIPT.slice(reload.index, reload.index + 900);
    if (tail.indexOf("needConfirm(") < 0) {
      problems.push("「恢复默认」没有二次确认 —— 一键会覆盖掉全部手动配置，不能手滑");
    }
    if (tail.indexOf("toast(") < 0) {
      problems.push("「恢复默认」成功/失败都没有提示");
    }
  }

  // --- 记忆列表：序号删除后要自愈；图谱「最多 N 条」要有应用入口 ---
  if (SCRIPT.indexOf("state.memOffset >= state.memTotal") < 0) {
    problems.push("删空最后一页后 offset 不会自愈 —— 页码会显示成「21-20 / 20」");
  }
  if (SCRIPT.indexOf("state.memOffset + index + 1") < 0) {
    problems.push("记忆列表没有连续序号（删除后不重排）");
  }
  if (HTML.indexOf('id="gp-apply"') < 0 || SCRIPT.indexOf('$("gp-apply").onclick') < 0) {
    problems.push("图谱「最多 N 条」没有应用按钮 —— 改了不生效，看着像没保存");
  }

  // --- 保存前的「有没有改动」比较：快照与比较必须共用同一套归一化 ---
  // 否则 list 控件（textarea）快照存字符串、比较给数组，永远对不上，
  // 表现就是「什么都没改，点保存却提示已保存 1 项」。
  if (SCRIPT.indexOf("state.settingsValues[el.dataset.path] = stValue(el)") < 0) {
    problems.push("stSnapshot 没走 stValue —— 快照与比较口径不一致，保存会误报改动");
  }
  if (SCRIPT.indexOf("if (!stSame(current, state.settingsValues[path]))") < 0) {
    problems.push("stChanged 没走 stValue/stSame —— 会把没改的项当成改了");
  }

  // --- 曲线取点：三个函数必须有调用点（以前是孤儿，「拖点改历史」等于没接上）---
  if (SCRIPT.indexOf("showPick(pointAt(") < 0) {
    problems.push("曲线上取点的入口没绑（pointAt/showPick 成了孤儿，点曲线没反应）");
  }
  if (SCRIPT.indexOf('pickAction(btn.getAttribute("data-act"))') < 0) {
    problems.push("取点操作条的按钮没接事件（pickAction 没人调）");
  }

  // --- 冗余守卫：这轮清掉的死代码不许复活 ---
  for (const dead of ["chr_newline", "thStrip", "BG_KEY", "bgCount", "bg-layer", "bg-veil", "glass-blur"]) {
    if (SCRIPT.indexOf(dead) >= 0 || HTML.indexOf(dead) >= 0) {
      problems.push("死代码复活了：" + dead);
    }
  }
  if (HTML.indexOf('data-theme="auto"') >= 0) {
    problems.push("data-theme=auto 的主题分支复活了（主题切换已删，只有 dark）");
  }

  // --- 主题：按钮删掉、默认暗色 ---
  if (HTML.indexOf('data-theme="dark"') < 0) {
    problems.push("默认主题不是暗色");
  }
  if (SCRIPT.indexOf("cycleTheme") >= 0 || SCRIPT.indexOf("applyTheme") >= 0) {
    problems.push("主题切换函数还在（按反馈已删）");
  }

  // --- 好感度收尾 + 情绪按钮 ---
  if (SCRIPT.indexOf('name: "亲昵"') < 0) {
    problems.push("一键改成里缺「亲昵」");
  }
  // --- 一键改成：锚点必须是后端情绪原型的原值，且按钮来自情绪包 ---
  if (SCRIPT.indexOf("state.data.prototypes") < 0) {
    problems.push("一键改成没用后端回的 prototypes —— 前端写死的数值迟早和 model.py 对不上");
  }
  const snap = /var MOOD_PRESETS_FALLBACK = \[([\s\S]*?)\];/.exec(SCRIPT);
  if (!snap) {
    problems.push("缺 MOOD_PRESETS_FALLBACK 兜底列表");
  } else {
    const entries = [...snap[1].matchAll(
      /\{ name: "([^"]+)", emoji: "([^"]+)", p: (-?[\d.]+), a: (-?[\d.]+), d: (-?[\d.]+) \}/g
    )].map((m) => ({ name: m[1], emoji: m[2], p: Number(m[3]), a: Number(m[4]), d: Number(m[5]) }));
    if (entries.length !== PROTO_TYPES.length) {
      problems.push(
        "一键改成兜底只有 " + entries.length + " 种，model.py 有 "
        + PROTO_TYPES.length + " 种 —— 少了情绪按钮"
      );
    }
    PROTO_TYPES.forEach((proto, i) => {
      const e = entries[i];
      if (!e) return;
      if (e.name !== proto.name || e.emoji !== proto.emoji
          || e.p !== proto.p || e.a !== proto.a || e.d !== proto.d) {
        problems.push(
          "一键改成第 " + (i + 1) + " 条与 model.py 不一致：页面 "
          + e.name + "/" + e.emoji + "/" + [e.p, e.a, e.d].join(",")
          + " vs 代码 " + proto.name + "/" + proto.emoji + "/"
          + [proto.p, proto.a, proto.d].join(",")
        );
      }
    });
    // 每个按钮发出去的锚点，分类必须落回它自己（最近邻）
    const dist = (x, y) => Math.hypot(x.p - y.p, x.a - y.a, x.d - y.d);
    for (const anchor of PROTO_TYPES) {
      let best = PROTO_TYPES[0];
      let bd = Infinity;
      for (const proto of PROTO_TYPES) {
        const d = dist(anchor, proto);
        if (d < bd) { bd = d; best = proto; }
      }
      if (best.key !== anchor.key) {
        problems.push(
          "锚点「" + anchor.name + "」会分类成「" + best.name + "」—— 数值离别人更近"
        );
      }
    }
  }
  if (SCRIPT.indexOf("已读取当前值") < 0) {
    problems.push("「读取当前值」没有成功反馈，点了像没反应");
  }
  if (SCRIPT.indexOf("乐观更新") < 0) {
    problems.push("「冻结」没有乐观更新，点了要等整轮刷新才看到变化");
  }
  if (SCRIPT.indexOf("和人的关系") >= 0) {
    problems.push("「和人的关系」的渲染代码还在");
  }
  if (SCRIPT.indexOf("gpEnterSelectMode") >= 0 && SCRIPT.indexOf("function gpEnterSelectMode") < 0) {
    problems.push("gpEnterSelectMode 被引用但没定义");
  }

  // --- 图谱：会话条件下推到 SQL（否则 LIMIT 先截断，图上是空的）---
  const graphSrc = readFileSync(join(HERE, "..", "mind", "graph.py"), "utf8");
  if (graphSrc.indexOf("session_id=session_id,") < 0) {
    problems.push("graph.build_graph 没把会话条件下推到 query_page —— 图谱会不跟会话联动");
  }

  if (problems.length) {
    console.log("❌ 本轮改造有遗漏：");
    problems.forEach((p) => console.log("   - " + p));
    process.exit(1);
  }
  console.log("✅ 来源筛选/通用勾选/批量改作用域/列表勾选/图谱会话下推 都在");
  console.log("✅ 分组折叠已绑定、恢复默认有二次确认与反馈、主题固定暗色、点人方块已删");
}


// ---- 静态检查：强度必须是「可调的主动产物」而不是情绪副产物 ----
{
  const problems = [];
  if (HTML.indexOf('id="si"') < 0) problems.push("缺强度滑块 #si");
  if (HTML.indexOf('id="siv"') < 0) problems.push("缺强度滑块读数 #siv");
  if (SCRIPT.indexOf("emotion/intensity") < 0) problems.push("强度滑块没有提交到 emotion/intensity");
  if (SCRIPT.indexOf("intensity_curve") < 0) problems.push(
    "曲线面板没有画强度曲线（intensity_curve 没被读）");
  if (SCRIPT.indexOf("emotion_intensity") < 0) problems.push(
    "会话下拉框没读新的强度模型 emotion_intensity");
  if (SCRIPT.indexOf("intensityPercent(c.intensity)") < 0) problems.push(
    "右上角模块还在显示旧口径（PAD 推导）的强度");
  if (HTML.indexOf("强度 I（右轴 0~1）") < 0) problems.push("图例里没有强度曲线");
  if (HTML.indexOf('id="sf"') < 0) problems.push("缺强度基准值（衰减最低值）滑块 #sf");
  if (SCRIPT.indexOf("emotion.intensity.floor") < 0) problems.push("基准值滑块没接到配置项 emotion.intensity.floor");
  if (SCRIPT.indexOf('"基准 " + Math.round') < 0) problems.push("曲线上没有画基准线（0% 的位置会看不出来）");
  if (SCRIPT.indexOf("Math.round(v * 100) + \"%\"") < 0) problems.push("强度右轴没有 0%/100% 刻度");
  if (SCRIPT.indexOf("yI(") < 0) problems.push("强度轴映射没了（0% 必须在最底下、100% 在最顶上）");
  if (SCRIPT.indexOf("function drawIntensityCurve") < 0) problems.push("强度曲线没有独立绘制函数（PAD 没数据时会跟着一起消失）");
  const drawAt = SCRIPT.indexOf("function drawChart()");
  const earlyAt = SCRIPT.indexOf("if (!d || !d.curve || d.curve.length < 2)", drawAt);
  const axisAt = SCRIPT.indexOf("强度轴（右轴 0~1）", drawAt);
  if (drawAt < 0 || earlyAt < 0 || axisAt < 0 || axisAt > earlyAt) {
    problems.push("强度轴/基准线画在了空数据早退之后 —— 没数据时基线会整块消失（就是「基线乱跑」）");
  }
  if (problems.length) {
    console.log("❌ 强度改造有遗漏：");
    problems.forEach((p) => console.log("   - " + p));
    process.exit(1);
  }
  console.log("✅ 强度滑块 / 强度曲线 / 右上角新强度 / 下拉框强度 都接上了");
}

// ---- 静态检查：每个按钮都要有绑定、点了要有返回；导出要给出文件位置 ----
{
  const problems = [];
  const buttons = [...HTML.matchAll(/<button\b[^>]*\bid="([^"]+)"/g)].map((m) => m[1]);
  const uniq = [...new Set(buttons)];
  // 页签是用循环绑的（$("tab-" + name)），单独认这一种写法
  const tabsBound = SCRIPT.indexOf('["emotion", "memory", "graph", "settings"].forEach') >= 0;
  for (const id of uniq) {
    const bound = SCRIPT.indexOf('$("' + id + '").onclick') >= 0
      || SCRIPT.indexOf('$("' + id + '").onchange') >= 0
      || (id.indexOf("tab-") === 0 && tabsBound);
    if (!bound) problems.push("按钮没绑事件：#" + id);
  }
  // 关键按钮必须点了有提示（toast / 结果行）
  const feedback = [
    ['$("refresh").onclick', '已刷新'],
    ['$("mg-reload").onclick', '管理数据已刷新'],
    ['$("pv-reload").onclick', '隐私数据已刷新'],
    ['function exportProfile', '→ 文件'],
    ['$("msearch").onclick', 'memoryPageHint'],
    ['$("mprev").onclick', 'memoryPageHint'],
    ['$("mnext").onclick', 'memoryPageHint'],
  ];
  for (const [needle, must] of feedback) {
    const at = SCRIPT.indexOf(needle);
    if (at < 0) { problems.push("找不到处理器：" + needle); continue; }
    if (SCRIPT.slice(at, at + 2500).indexOf(must) < 0) {
      problems.push("点了没有返回：" + needle + "（缺 " + must + "）");
    }
  }
  // 导出：要把路径摆到界面上，并展示预览
  if (SCRIPT.indexOf('$("pv-json")') < 0) problems.push("导出后没有展示内容的区域");
  if (SCRIPT.indexOf('（预览截断') < 0) problems.push("导出预览没有截断保护");
  // 集体冻结：按钮 + 后端动作都要在
  if (HTML.indexOf('id="mg-freeze"') < 0) problems.push("缺「冻结全部会话」按钮");
  if (SCRIPT.indexOf('action: "freeze_all"') < 0) problems.push("集体冻结没接到 freeze_all");
  // 设置保存：没存上的项必须报出来
  if (SCRIPT.indexOf('not_saved') < 0) problems.push("保存后没有核对「没存上的项」");
  // 记忆清理的类型下拉不能每次刷新都弹回「全部类型」
  if (SCRIPT.indexOf('state.mgKind') < 0) problems.push("类型下拉的选择没有被记住");
  // 心境：独立卡片 + 起因劈两半 + 迷你条/趋势/依据都要真的被渲染
  if (HTML.indexOf('id="mood-card"') < 0) problems.push("缺独立的「心境」卡 #mood-card");
  if (HTML.indexOf('id="mood-cause"') < 0) problems.push("起因没有劈两半（缺 #mood-cause）");
  if (HTML.indexOf('id="moodbar-fill"') < 0) problems.push("心境迷你条没了");
  if (HTML.indexOf('class="rightcol"') < 0) problems.push("右列没改成 flex（底部对不齐/会压住按钮）");
  ["mood-name", "mood-trend", "mood-mag", "mood-since", "mood-reason"].forEach(function (id) {
    if (SCRIPT.indexOf(id) < 0) problems.push("心境卡没填数据：#" + id);
  });
  if (SCRIPT.indexOf("trend_text") < 0) problems.push("趋势（G2）没被渲染");
  // ---- 调节心境：必须和「操纵情绪」不一样（偏移维度 + 心境描述词）----
  if (HTML.indexOf('id="mood-ops"') < 0) problems.push("缺「调节心境」卡 #mood-ops");
  ["smp", "sma", "smd", "mood-words", "mood-apply", "mood-load", "mood-reset", "mood-ops-hint"].forEach(function (id) {
    if (HTML.indexOf('id="' + id + '"') < 0) problems.push("调节心境缺控件 #" + id);
  });
  ["底色", "能量", "底气"].forEach(function (word) {
    if (HTML.indexOf(word) < 0) problems.push("心境滑杆没有用心境自己的语义（缺「" + word + "」）");
  });
  if (HTML.indexOf('id="smp" min="-0.6"') < 0) problems.push("心境滑杆不是偏移语义（±0.6）");
  if (SCRIPT.indexOf("renderMoodWords") < 0) problems.push("心境的一键改成没渲染（mood_words）");
  if (SCRIPT.indexOf("emotion/mood") < 0) problems.push("调节心境没接到后端 emotion/mood");
  const wordFnAt = SCRIPT.indexOf("function renderMoodWords");
  if (wordFnAt >= 0 && SCRIPT.slice(wordFnAt, wordFnAt + 1400).indexOf("moodPresets()") >= 0) {
    problems.push("心境的一键改成还在用情绪原型（该用心境描述词 mood_words）");
  }
  // ---- 最近心境事件 + 两个列表限高滚动 ----
  if (HTML.indexOf('id="mood-events"') < 0) problems.push("缺「最近心境事件」列表 #mood-events");
  if (SCRIPT.indexOf("mood_events") < 0) problems.push("没渲染最近心境事件");
  if (HTML.indexOf("eventlist") < 0 || SCRIPT.indexOf("还没有心境波动记录") < 0) {
    problems.push("事件列表没有限高滚动（eventlist），页面会被撑长");
  }
  // 两张事件卡要**均分**左列剩余空间，且都保留滚轮滚动
  const cardA = HTML.indexOf('id="events-card"') >= 0 && HTML.indexOf("flex:1 1 0;min-height:0", HTML.indexOf('id="events-card"')) >= 0;
  const cardB = HTML.indexOf('id="mood-events-card"') >= 0 && HTML.indexOf("flex:1 1 0;min-height:0", HTML.indexOf('id="mood-events-card"')) >= 0;
  if (!cardA || !cardB) problems.push("两张事件卡没有均分左列剩余空间（都要 flex:1 1 0）");
  if (HTML.indexOf("eventcard") < 0 || HTML.indexOf("overflow-y: auto") < 0) {
    problems.push("事件卡缺 .eventcard / 列表缺 overflow-y:auto（滚轮滚动）");
  }
  // 限高必须用**视口单位**：写死 260px 这种小数值时，卡片一被左列拉高
  // （报过的「滑动条占不满」），列表下面就空出一大块。
  const listRule = /\.eventlist\s*\{[^}]*\}/.exec(HTML);
  if (!listRule) {
    problems.push("找不到 .eventlist 的样式规则");
  } else if (!/max-height\s*:\s*[\d.]+vh/.test(listRule[0])) {
    problems.push(".eventlist 的 max-height 不是 vh（固定 px 会填不满卡片）：" + listRule[0]);
  }
  // 心境词的 emoji 必须是老版本就有的（🫙🫶 这类会画成方框 □）——
  // 只看 MOOD_WORDS 表本身，注释里拿它们当反例是允许的
  const moodwordsSrc = readFileSync(join(HERE, "..", "mind", "emotion", "moodwords.py"), "utf8");
  const tableStart = moodwordsSrc.indexOf("MOOD_WORDS: tuple");
  const tableEnd = moodwordsSrc.indexOf("MOOD_WORDS_BY_KEY");
  const wordTable = tableStart >= 0 && tableEnd > tableStart
    ? moodwordsSrc.slice(tableStart, tableEnd) : moodwordsSrc;
  ["🫙", "🫶", "🫠", "🫂", "🥹"].forEach(function (e) {
    if (wordTable.indexOf(e) >= 0) problems.push("心境词用了老系统画不出来的 emoji：" + e);
  });
  // 换过的两个必须是真的（防止有人改回去）
  if (wordTable.indexOf("💭") < 0 || wordTable.indexOf("🤗") < 0) {
    problems.push("心事重重/有点黏人的 emoji 没换成老系统能画的（💭 / 🤗）");
  }
  // 滑杆读数：fmtNum 已经会补「+」，再手动拼一个就成了 ++0.22
  if (SCRIPT.indexOf('(v >= 0 ? "+" : "") + fmtNum') >= 0) {
    problems.push("调节心境的读数出现双加号（fmtNum 已自带正号）");
  }
  // ---- 因果链（A1/B3/C1/D2/E2/F1/G1）----
  if (SCRIPT.indexOf("SRC_ICON") < 0 || SCRIPT.indexOf("SRC_TEXT") < 0) problems.push("事件流没有来源分层（D2）");
  if (SCRIPT.indexOf("e.message") < 0) problems.push("事件流不显示原话（A1）");
  if (SCRIPT.indexOf("ci.text") < 0) problems.push("情绪起因没按结构读（F1）");
  if (SCRIPT.indexOf("cause_display") < 0) problems.push("心境起因没用人话版（E2）");
  const appraisalSrc = readFileSync(join(HERE, "..", "mind", "emotion", "appraisal.py"), "utf8");
  if (appraisalSrc.indexOf('cause=" + ".join') >= 0) problems.push("起因还在机械拼串（B3 没做）");
  if (appraisalSrc.indexOf("QUESTION_WORDS") < 0) problems.push("提问规则没拆（C1 没做）");
  const engineSrc = readFileSync(join(HERE, "..", "mind", "emotion", "engine.py"), "utf8");
  if (engineSrc.indexOf("顺着当时的心情") < 0) problems.push("自回读文案还没弱化（G1）");
  if (engineSrc.indexOf('f"她自己的语气（') >= 0) problems.push("还在写「她自己的语气（…）」（G1 没做）");
  if (engineSrc.indexOf("_capture_mood_cause(\"被手动设定\")") >= 0 ||
      engineSrc.indexOf("_capture_mood_cause(\"好久没人说话了\")") >= 0) {
    problems.push("心境起因还在抓「手动设定 / 没人说话」（E2 没做）");
  }
  if (engineSrc.indexOf("只保留最近三轮") < 0) problems.push("原话没有三轮滚动丢弃（A1 没做）");
  // ---- 心境冻结 + 曲线点编辑 + 满量程迷你条 + 底气先判 ----
  if (HTML.indexOf('id="mood-freeze"') < 0) problems.push("缺「冻结心境」按钮");
  if (SCRIPT.indexOf("d.mood.frozen") < 0) problems.push("面板没读心境冻结状态（mood.frozen）");
  if (engineSrc.indexOf("def freeze_mood") < 0 || engineSrc.indexOf("def unfreeze_mood") < 0) {
    problems.push("后端没有 freeze_mood / unfreeze_mood");
  }
  const frozenGuards = engineSrc.split("if not session.frozen_mood:").length - 1;
  if (frozenGuards < 3) {
    problems.push("心境冻结没封住所有旁路（直接 lerp 心境的地方少了，当前 " + frozenGuards + " 处）");
  }
  if (engineSrc.indexOf("def edit_intensity_marks") < 0) {
    problems.push("后端没有 edit_intensity_marks —— 取点动不到强度");
  }
  if (SCRIPT.indexOf("点 · 强度 ") < 0) problems.push("取点操作没反馈强度锚点（改/删了几个强度点）");
  if (SCRIPT.indexOf("mo.bar") < 0) problems.push("迷你条没用满量程 bar（到不了最左/最右）");
  const moodlabelSrc = readFileSync(join(HERE, "..", "mind", "emotion", "moodlabel.py"), "utf8");
  if (moodlabelSrc.indexOf("MOOD_BAR_SCALE") < 0 || moodlabelSrc.indexOf("def bar_value") < 0) {
    problems.push("moodlabel 缺满量程 bar_value / MOOD_BAR_SCALE");
  }
  if (moodlabelSrc.indexOf("if d >= 0.30 and a >= -0.15") < 0) {
    problems.push("direction() 不是底气先判 —— 拉底气还是不会换词");
  }
  const wordCount = moodwordsSrc.split("MoodWord(").length - 1;
  if (wordCount < 16) problems.push("心境描述词太少了（当前 " + wordCount + " 个，应 ≥16）");
  if (problems.length) {
    console.log("❌ 面板按钮审计有遗漏：");
    problems.forEach((p) => console.log("   - " + p));
    process.exit(1);
  }
  console.log("✅ " + uniq.length + " 个按钮都有绑定；刷新/查询/翻页/导出都有返回；集体冻结与「没存上报错」已接上");
}
const PLUGIN = "astrbot_plugin_ai_mind";
//: 插件实际注册的子路径（和后端 _route_defs 保持一致）
const KNOWN_ROUTES = [
  PLUGIN + "/sessions", PLUGIN + "/settings", PLUGIN + "/settings/save",
  PLUGIN + "/settings/reset",
  PLUGIN + "/privacy", PLUGIN + "/privacy/save", PLUGIN + "/privacy/deny",
  PLUGIN + "/privacy/forget", PLUGIN + "/privacy/export",
  PLUGIN + "/manage", PLUGIN + "/manage/memory", PLUGIN + "/manage/data",
  PLUGIN + "/emotion", PLUGIN + "/emotion/set", PLUGIN + "/emotion/mood", PLUGIN + "/emotion/intensity",
  PLUGIN + "/emotion/point",
  PLUGIN + "/emotion/reset", PLUGIN + "/emotion/curve/clear",
  PLUGIN + "/emotion/freeze",
  PLUGIN + "/memory", PLUGIN + "/memory/add", PLUGIN + "/memory/update",
  PLUGIN + "/memory/delete", PLUGIN + "/memory/batch",
  PLUGIN + "/graph", PLUGIN + "/_echo"
];
const SESSIONS_OK = { sessions: [{ key: "s1", label: "私聊 · 1", group: false, samples: 3,
  emotion: "平静", emotion_emoji: "🙂", emotion_level: 3, emotion_intensity: 0.4 }], stats: {} };
//: 情绪页要的是一整包数据（renderEmotion 会一口气读很多字段，缺一个就抛错）
const EMOTION_OK = {
  session: { key: "s1", label: "私聊 · 1", group: false },
  now: 0, since: 0, hours: 72,
  current: {
    p: 0.35, a: -0.1, d: 0.2, label: "平静", emoji: "🙂",
    // 强度来自**会话里的独立状态**（0.4 → 3/5 明显），不再是 PAD 长度算的副产物
    level: 3, level_name: "明显", intensity: 0.4, pad_intensity: 0.18,
    cause: { text: "被查户口了（还顺带：被随口问了一句）", t: 0, ago: "2 分钟前",
             message: "你几岁了？住在哪", delta: { p: -0.3, a: 0.46, d: -0.19 },
             source: "user", primary: "nosy", secondary: ["question"] },
    crisis: false
  },
  mood: { p: 0.1, a: -0.2, d: 0.1, key: "joy", label: "有点开心", name: "开心",
    degree: "有点", emoji: "😊", reason: "愉悦比平时高 0.20；唤醒比平时高 0.10",
    cause: "被夸了", cause_ago: "5 分钟前", cause_display: "被夸了（5 分钟前）", magnitude: 0.24, score: 0.23, bar: 0.38,
    since: 0, since_ago: "-", trend: "up", trend_text: "↗ 在变好", frozen: false },
  baseline: { p: 0.08, a: 0.22, d: 0.3 },
  params: { emotion_half_life: 240, mood_half_life: 21600,
    intensity_decay: "exponential", intensity_half_life: 900,
    intensity_floor: 0.15, intensity_gain: 0.55 },
  intensity_curve: [ { t: 0, v: 0.62 }, { t: 1, v: 0.4 } ],
  prototypes: PROTO_TYPES,
  idle_seconds: 12,
  curve_source: "samples",
  sample_count: 5,
  sample_total: 5,
  samples: [],
  events: [],
  mood_events: [],
  mood_words: [
    { key: "calm", label: "平静如常", emoji: "😌", offset: [0, 0, 0], target: "calm" },
    { key: "content", label: "心满意足", emoji: "🥰", offset: [0.38, -0.18, 0.14],
      target: "content" },
    { key: "down", label: "低气压", emoji: "🌧️", offset: [-0.3, -0.22, -0.1],
      target: "down" }
  ],
  users: [],
  history_count: 0,
  frozen: false,
  diag: {},
  db_check: { write: true, error: "", rows: 5, ok: true },
  samples_broken: false,
  samples_error: "",
  plugin_disabled: "",
  settings: { curve_hours: 72, sample_epsilon: 0.004, heartbeat_seconds: 300 }
};
const GRAPH_OK = {
  nodes: [
    { id: "m:1", type: "memory", label: "他喜欢喝冰美式", weight: 1, cluster: 1,
      data: { owner: "1000000001", scope: "user", kind: "preference" } },
    { id: "m:2", type: "memory", label: "她下周要去上海出差", weight: 1, cluster: 2,
      data: { owner: "555555", scope: "user", kind: "event" } },
    { id: "u:1000000001", type: "user", label: "1000000001", weight: 1.4, cluster: 3,
      data: { uid: "1000000001" } }
  ],
  edges: [], clusters: [{ id: 1, name: "C/01", size: 1, keywords: ["冰美式"] }],
  stats: { memories_total: 2, memories_shown: 2, nodes: 3, edges: 0, clusters: 1, sessions: 1, keywords: 1 },
  filters: { session: "s1" }
};
//: 通用配置：renderSettings 要按 groups 渲染出带 data-fold / data-body 的分组卡
const SETTINGS_OK = {
  writable: true,
  config_file: "_conf_schema.json",
  block_count: 2,
  custom_count: 0,
  groups: [
    { key: "emotion", label: "情绪", hint: "", fields: [
      { path: "emotion.enabled", key: "enabled", label: "情绪开关",
        hint: "", type: "bool", options: [], value: true, default: true },
      // list 控件（textarea）：快照与比较的归一化必须一致，
      // 否则什么都没改点保存也会报「已保存 1 项」
      { path: "emotion.persona.custom_rules", key: "custom_rules", label: "自定义情绪规则",
        hint: "", type: "list", options: [], value: [], default: [] }
    ] },
    { key: "memory", label: "长期记忆", hint: "", fields: [
      { path: "memory.enabled", key: "enabled", label: "记忆开关",
        hint: "", type: "bool", options: [], value: true, default: true }
    ] }
  ],
  overrides: {}
};
//: 记忆列表（按会话过滤 + 手动/自动来源）
const MEMORY_OK = {
  items: [
    { id: 1, content: "他喜欢喝冰美式", kind: "preference", scope: "user",
      owner_id: "1000000001", session_id: "", pinned: 1, sensitive: 0,
      importance: 1, hit_count: 0, source: "auto", ago: "刚刚",
      created_at: 1, updated_at: 1, keywords: ["冰美式"] },
    { id: 2, content: "555555 说下周去上海出差", kind: "event", scope: "user",
      owner_id: "555555", session_id: "", pinned: 0, sensitive: 0,
      importance: 1, hit_count: 0, source: "manual", ago: "2 小时前",
      created_at: 2, updated_at: 2, keywords: ["出差"] }
  ],
  total: 2, offset: 0, limit: 20, session_id: "s1",
  stats: { total: 2, pinned: 1, sensitive: 0, embedded: 0 }
};

function makeCanvasCtx() {
  const noop = () => {};
  return new Proxy(
    { measureText: () => ({ width: 10 }), canvas: {} },
    { get: (t, k) => (k in t ? t[k] : noop), set: () => true }
  );
}

function makeElement(id) {
  return {
    id, value: "", textContent: "", innerHTML: "", className: "", disabled: false,
    dataset: {}, style: {},
    classList: { add() {}, remove() {}, contains: () => false },
    appendChild() {},
    // 迷你 DOM：renderSettings 是往 innerHTML 塞字符串的，
    // 不解析的话「分组折叠」这条委托链根本测不到。
    querySelector(sel) {
      const m = /\[data-body='(\d+)'\]/.exec(String(sel));
      if (!m) return null;
      this._stBodies = this._stBodies || {};
      if (!this._stBodies[m[1]]) this._stBodies[m[1]] = { style: { display: "block" } };
      return this._stBodies[m[1]];
    },
    querySelectorAll(sel) {
      if (/\[data-fold\]/.test(String(sel))) {
        if (this._lastFoldHTML !== this.innerHTML) {
          this._lastFoldHTML = this.innerHTML;
          this._stFolds = {};
          const re = /data-fold='(\d+)'/g;
          const html = String(this.innerHTML || "");
          let m;
          while ((m = re.exec(html)) !== null) {
            const idx = m[1];
            this._stFolds[idx] = { getAttribute: () => idx, textContent: "折叠" };
          }
        }
        return Object.values(this._stFolds || {});
      }
      return [];
    },
    addEventListener(type, fn) {
      // 记下有哪些监听器 —— 图谱画布靠它们响应拖动/缩放/悬停/点选
      (this._events || (this._events = {}))[type] = fn;
    },
    /** 模拟一次事件派发：只有 addEventListener 存下来的才能触发 */
    fire(type, ev) {
      const fn = this._events && this._events[type];
      if (typeof fn === "function") return fn(ev || {});
      return undefined;
    },
    closest: () => null,
    getContext: () => makeCanvasCtx(),
    clientWidth: 800, clientHeight: 300, width: 0, height: 0,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 300 })
  };
}

/** 跑一次页面脚本，返回它的可观测行为。 */
async function run({ bridge, fetchImpl, origin = "http://astrbot.local" }) {
  const elements = new Map();
  const fetched = [];
  const bridgeCalls = [];
  // 模拟后端的情绪冻结状态：POST 改，GET 带回来（这样按钮的乐观更新能被对账）
  let frozen = false;

  const sandbox = {
    console,
    setTimeout: (fn) => setTimeout(fn, 0),
    clearTimeout: (t) => clearTimeout(t),
    Promise, JSON, Math, Date, Number, String, Object, Array, Error, RegExp,
    encodeURIComponent, decodeURIComponent,
    devicePixelRatio: 1
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.location = { origin };
  sandbox.document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    createElement: (tag) => makeElement(tag),
    querySelector: () => null,
    // 迷你 DOM：把 #mlist 里的复选框解析成可交互的替身。
    // renderMemories 是往 innerHTML 塞字符串的，不解析的话
    // 「勾选 → 写回 state.selected」这条链根本测不到。
    querySelectorAll(sel) {
      const s = String(sel);
      // 分组折叠按钮渲染在 #st-groups 里，转交给那个元素的迷你实现
      if (/\[data-fold\]/.test(s)) {
        const host = elements.get("st-groups");
        return host ? host.querySelectorAll(s) : [];
      }
      if (!/memsel/.test(s)) return [];
      const list = elements.get("mlist");
      if (!list) return [];
      if (list._lastHTML !== list.innerHTML) {
        list._lastHTML = list.innerHTML;
        list._boxes = {};
        const re = /<input[^>]*class="memsel"[^>]*data-id="(\d+)"([^>]*)>/g;
        let m;
        while ((m = re.exec(list.innerHTML)) !== null) {
          list._boxes[m[1]] = {
            dataset: { id: m[1] },
            checked: /\bchecked\b/.test(m[2] || ""),
            classList: { contains: (c) => c === "memsel" }
          };
        }
      }
      return Object.values(list._boxes || {});
    },
    addEventListener() {},
    documentElement: {
      getAttribute: () => "light",
      setAttribute() {},
      // 真浏览器里 style 是 CSSStyleDeclaration；这里给个够用的替身，
      // 否则面板里 setProperty("--bg-blur", ...) 会把脚本带崩
      style: { setProperty() {}, removeProperty() {}, getPropertyValue: () => "" }
    }
  };
  sandbox.getComputedStyle = () => ({ getPropertyValue: () => "#888888" });
  sandbox.addEventListener = () => {};

  if (bridge) {
    sandbox.AstrBotPluginPage = {
      ready: () => Promise.resolve({}),
      getContext: () => ({}),
      getLocale: () => "zh-CN",
      t: (k, f) => f,
      onContext: () => () => {},
      apiGet(endpoint, params) {
        bridgeCalls.push({ method: "GET", endpoint, params });
        // 复现真实宿主的校验：endpoint 里带 ? 一律直接拒掉
        if (String(endpoint).indexOf("?") >= 0) {
          return Promise.reject(new Error("Plugin bridge endpoint is invalid."));
        }
        const verdict = bridge(endpoint);
        if (verdict === true) {
          if (endpoint.indexOf("sessions") >= 0) return Promise.resolve(SESSIONS_OK);
          if (endpoint.endsWith("/graph")) return Promise.resolve(GRAPH_OK);
          if (endpoint.endsWith("/memory")) return Promise.resolve(MEMORY_OK);
          if (endpoint.endsWith("/settings")) return Promise.resolve(SETTINGS_OK);
          if (endpoint.endsWith("/emotion")) {
            return Promise.resolve(Object.assign({}, EMOTION_OK, { frozen: frozen }));
          }
          return Promise.resolve({ current: null });
        }
        return Promise.reject(new Error(verdict || "bridge rejected"));
      },
      apiPost(endpoint, body) {
        bridgeCalls.push({ method: "POST", endpoint, body });
        if (String(endpoint).indexOf("?") >= 0) {
          return Promise.reject(new Error("Plugin bridge endpoint is invalid."));
        }
        const verdict = bridge(endpoint);
        if (verdict !== true) return Promise.reject(new Error(verdict || "bridge rejected"));
        if (String(endpoint).endsWith("/emotion/freeze")) frozen = !!(body && body.frozen);
        if (String(endpoint).endsWith("/settings/reset")) {
          return Promise.resolve(Object.assign({}, SETTINGS_OK, {
            reset: ["enabled", "emotion.enabled", "memory.enabled"]
          }));
        }
        return Promise.resolve({ ok: true });
      }
    };
  }

  sandbox.fetch = (url) => {
    fetched.push(String(url));
    const verdict = fetchImpl ? fetchImpl(String(url)) : "no fetch allowed";
    if (verdict === true) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(String(url).indexOf("sessions") >= 0 ? SESSIONS_OK : { current: null })
      });
    }
    return Promise.reject(new Error(verdict));
  };

  const context = vm.createContext(sandbox);
  vm.runInContext(SCRIPT, context, { filename: "panel.js" });
  await new Promise((r) => setTimeout(r, 30));

  const status = elements.get("api-status");
  return {
    status: status ? status.textContent : "",
    statusBad: status ? status.className.indexOf("err") >= 0 : false,
    diag: sandbox.__aiMindDiag || null,
    fetched,
    bridgeCalls,
    elements,
    // 页面脚本主动挂出来的状态（排查用），断言时要用
    state: sandbox.__aiMindState,
    queryAll: (sel) => sandbox.document.querySelectorAll(sel),
    // 懒创建的元素（只在某个回调里才 $() 到的）要靠它取
    el: (id) => sandbox.document.getElementById(id),
    toast: elements.get("toast") ? elements.get("toast").textContent : ""
  };
}

const cases = [
  {
    name: "查询参数走 params，绝不拼进 endpoint（宿主会校验 endpoint）",
    opts: {
      bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0,
      // 让 sessions 返回带 session 的数据，好触发后续带参数的 emotion 请求
      sessions: SESSIONS_OK
    },
    check: (r) => {
      const withQuery = r.bridgeCalls.filter((c) => String(c.endpoint).indexOf("?") >= 0);
      if (withQuery.length) return false;
      const emotionCall = r.bridgeCalls.find((c) => c.endpoint.indexOf("emotion") >= 0);
      if (!emotionCall) return false;
      // 参数必须以对象形式传进去
      return !!(emotionCall.params && emotionCall.params.hours);
    }
  },
  {
    name: "实测场景：宿主自己补 /api/plug/，只接受插件相对路径（v4.28.1 的行为）",
    opts: {
      bridge: (ep) => {
        // 忠实模拟 v4.28.1 的宿主 + 后端：
        //   宿主把端点拼成 /api/plug/<插件名>/<ep>
        //   后端取出 <插件名>/<ep> 拿去和"已注册路由"比对
        // 已注册路由是 /<插件名>/<子路径>，所以只有**纯子路径**能匹配上；
        // 再多带一层插件名就会变成 .../插件名/插件名/... 而落进兜底。
        const hostPath = "/api/plug/" + PLUGIN + "/" + ep;
        const pluginPath = hostPath.slice("/api/plug/".length);
        return KNOWN_ROUTES.indexOf(pluginPath) >= 0 ? true : "未找到该路由";
      }
    },
    check: (r) =>
      r.diag && r.diag.transport === "bridge" && r.diag.prefix === "" &&
      !r.statusBad &&
      !r.toast &&
      r.bridgeCalls.length > 0 &&
      // 面板启动时会先问一次面板模式，所以不能死认第一条；只要 sessions 走的是
      // 插件相对路径（不带查询串）就算通过
      r.bridgeCalls.some((call) => String(call.endpoint).indexOf("?") < 0 &&
        String(call.endpoint).endsWith("sessions"))
  },
  {
    name: "宿主原样发出：/api/plug/ 绝对路径也能用",
    opts: { bridge: (ep) => ep.indexOf("/api/plug/" + PLUGIN + "/") === 0 },
    check: (r) => r.diag && r.diag.transport === "bridge"
      && r.diag.prefix === "/api/plug/" + PLUGIN + "/" && !r.statusBad
  },
  {
    name: "桥只认绝对 /api/plug/ 时也能命中",
    opts: { bridge: (ep) => ep.indexOf("/api/plug/" + PLUGIN + "/") === 0 },
    check: (r) => r.diag && r.diag.transport === "bridge"
      && r.diag.prefix === "/api/plug/" + PLUGIN + "/" && !r.statusBad && !r.toast
  },
  {
    name: "桥只认 /plug/（父窗口自己补 /api）—— 应当跳过第一个候选",
    opts: { bridge: (ep) => ep.indexOf("/plug/" + PLUGIN + "/") === 0 },
    check: (r) => r.diag && r.diag.transport === "bridge"
      && r.diag.prefix === "/plug/" + PLUGIN + "/" && !r.statusBad && !r.toast
  },
  {
    name: "桥只认 /plugins/extensions/（新版本路由）",
    opts: { bridge: (ep) => ep.indexOf("/plugins/extensions/" + PLUGIN + "/") === 0 },
    check: (r) => r.diag && r.diag.transport === "bridge"
      && r.diag.prefix === "/plugins/extensions/" + PLUGIN + "/"
      && !r.statusBad && !r.toast
  },
  {
    name: "没有桥、同源 fetch 可用 —— 应当自动回退到 fetch",
    opts: { bridge: null, fetchImpl: (u) => u.indexOf("/api/plug/" + PLUGIN + "/") >= 0 },
    check: (r) => r.diag && r.diag.transport === "fetch"
      && r.fetched.some((u) => u.indexOf("/api/plug/" + PLUGIN + "/") >= 0)
      && !r.statusBad && !r.toast
  },
  {
    name: "桥和 fetch 全不通 —— 必须给出可诊断的失败信息而不是静默",
    opts: { bridge: () => "接口不存在", fetchImpl: () => "网络错误" },
    check: (r) =>
      r.statusBad &&
      r.status.includes("接口不通") &&
      r.status.includes("接口不存在") &&
      r.status.includes("种组合都失败") &&
      r.toast.includes("加载失败") &&
      r.toast.length < 120
  },
  {
    name: "不透明源（origin=null）时不该尝试 fetch",
    opts: {
      origin: "null",
      bridge: (ep) => ep.indexOf("/api/plug/" + PLUGIN + "/") === 0
    },
    check: (r) => r.fetched.length === 0 && !r.statusBad
      && r.diag && r.diag.transport === "bridge"
  },
  // ---- 回归：切页签必须顺带拉数据 ----
  // 真踩过：切页签只调了 setView，没调加载器，于是记忆图谱整页空白。
  {
    name: "点「记忆图谱」页签会真的拉数据，并且只画选中会话",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      const tab = r.elements.get("tab-graph");
      if (!tab || typeof tab.onclick !== "function") throw new Error("记忆图谱页签没有 onclick");
      tab.onclick();
      await new Promise((x) => setTimeout(x, 40));
    },
    check: (r) => {
      const call = r.bridgeCalls.find((c) => String(c.endpoint).endsWith("/graph"));
      if (!call) return false;
      // 图谱必须带上顶部选中的会话（loadSessions 把它设成 s1）
      if (!call.params || call.params.session !== "s1") return false;
      // 光发请求还不够 —— 数据要真的画到统计条上
      const stats = r.elements.get("gp-stats");
      if (!stats || String(stats.innerHTML).indexOf("总记忆") < 0) return false;
      return !r.toast;
    }
  },
  {
    name: "切到「长期记忆」页签会按选中会话拉列表",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      const tab = r.elements.get("tab-memory");
      tab.onclick();
      await new Promise((x) => setTimeout(x, 40));
    },
    check: (r) => {
      const call = r.bridgeCalls.filter((c) => String(c.endpoint).endsWith("/memory") && !String(c.endpoint).endsWith("/memory/add"))
        .pop();
      return !!(call && call.params && call.params.session === "s1");
    }
  },
  {
    name: "图谱画布真的挂上了拖动/缩放/悬停/点选监听",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      r.elements.get("tab-graph").onclick();
      await new Promise((x) => setTimeout(x, 40));
    },
    check: (r) => {
      const ev = r.elements.get("graph")._events || {};
      const need = ["pointerdown", "pointermove", "pointerup", "wheel"];
      const missing = need.filter((k) => !ev[k]);
      if (missing.length) {
        console.log("     缺的监听：" + missing.join(","));
        return false;
      }
      return !r.toast;
    }
  },
  {
    name: "换个会话时，正在看的那一页会跟着重新拉",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      // 先停在图谱页，再换会话 —— 换完会话当前页要跟着重新拉
      r.elements.get("tab-graph").onclick();
      await new Promise((x) => setTimeout(x, 40));
      const sel = r.elements.get("session");
      sel.value = "s2";
      sel.onchange();
      await new Promise((x) => setTimeout(x, 40));
    },
    check: (r) => {
      const calls = r.bridgeCalls.filter((c) => String(c.endpoint).endsWith("/graph"));
      const last = calls[calls.length - 1];
      return !!(last && last.params && last.params.session === "s2");
    }
  },
  {
    name: "「手动 / 自动抽取」筛选会传给后端",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      r.elements.get("tab-memory").onclick();
      await new Promise((x) => setTimeout(x, 40));
      r.elements.get("msrc").value = "auto";
      r.elements.get("msearch").onclick();
      await new Promise((x) => setTimeout(x, 40));
    },
    check: (r) => {
      const call = r.bridgeCalls.filter((c) => String(c.endpoint).endsWith("/memory")).pop();
      if (!call || !call.params) return false;
      if (call.params.source !== "auto") {
        console.log("     source=" + JSON.stringify(call.params.source));
        return false;
      }
      if (call.params.owner !== undefined) {
        console.log("     不该再传 owner");
        return false;
      }
      return true;
    }
  },
  {
    name: "通用配置：分组「折叠」能收起，「恢复默认」有确认与反馈",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      r.elements.get("tab-settings").onclick();
      await new Promise((x) => setTimeout(x, 60));

      // ① 分组里的「折叠」按钮是动态渲染的，事件委托必须接住
      const groups = r.queryAll("[data-fold]");
      if (!groups.length) throw new Error("配置里没有分组折叠按钮");
      r.foldBtn = groups[0];
      const idx = r.foldBtn.getAttribute("data-fold");
      r.foldBody = r.el("st-groups").querySelector("[data-body='" + idx + "']");
      if (!r.foldBody) throw new Error("找不到分组内容 [data-body=" + idx + "]");
      r.foldBody.style.display = "block";
      // 事件委托绑定在容器上，手动派发一次 click
      const ev = {
        target: { closest: (sel) => (sel === "[data-fold]" ? r.foldBtn : null) }
      };
      r.el("st-groups").onclick(ev);
      r.folded = r.foldBody.style.display;
      r.foldLabel = r.foldBtn.textContent;

      // ② 恢复默认：第一次点 = 二次确认，第二次点才真的调 settings/reset
      r.elements.get("st-reload").onclick();
      r.confirmToast = String(r.elements.get("toast").textContent || "");
      r.elements.get("st-reload").onclick();
      await new Promise((x) => setTimeout(x, 80));
      r.reloadToast = String(r.elements.get("toast").textContent || "");
      r.resetCalls = r.bridgeCalls.filter(
        (c) => c.method === "POST" && String(c.endpoint).endsWith("/settings/reset")
      );
      r.stDirty = String(r.el("st-dirty").textContent || "");
    },
    check: (r) => {
      if (r.folded !== "none") {
        console.log("     折叠后 display=" + JSON.stringify(r.folded));
        return false;
      }
      if (r.foldLabel !== "展开") {
        console.log("     折叠按钮文字=" + JSON.stringify(r.foldLabel));
        return false;
      }
      if (r.confirmToast.indexOf("确认") < 0) {
        console.log("     恢复默认没有二次确认提示=" + JSON.stringify(r.confirmToast));
        return false;
      }
      if (r.reloadToast.indexOf("恢复默认") < 0) {
        console.log("     恢复默认反馈=" + JSON.stringify(r.reloadToast));
        return false;
      }
      if (r.resetCalls.length !== 1) {
        console.log("     settings/reset 调用次数=" + r.resetCalls.length);
        return false;
      }
      if (r.stDirty.indexOf("恢复默认") < 0) {
        console.log("     设置页提示行=" + JSON.stringify(r.stDirty));
        return false;
      }
      return true;
    }
  },
  {
    name: "什么都没改就点「保存改动」：提示没有改动，且不发请求",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      r.elements.get("tab-settings").onclick();
      await new Promise((x) => setTimeout(x, 60));
      r.elements.get("st-save").onclick();
      await new Promise((x) => setTimeout(x, 60));
      r.saveToast = String(r.elements.get("toast").textContent || "");
      r.saveCalls = r.bridgeCalls.filter(
        (c) => c.method === "POST" && String(c.endpoint).endsWith("/settings/save"));
    },
    check: (r) => {
      if (r.saveToast.indexOf("没有改动") < 0) {
        console.log("     保存反馈=" + JSON.stringify(r.saveToast));
        return false;
      }
      if (r.saveCalls.length) {
        console.log("     没改动也发了 " + r.saveCalls.length
          + " 次 settings/save：" + JSON.stringify(r.saveCalls[0].body));
        return false;
      }
      return true;
    }
  },
  {
    name: "「读取当前值」要有成功反馈，「冻结」要点完立刻变",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      // 页面初始化的 refreshAll 已经把情绪数据拉到了
      r.elements.get("load-cur").onclick();
      await new Promise((x) => setTimeout(x, 40));
      r.readToast = String(r.elements.get("toast").textContent || "");
      r.slider = String(r.elements.get("sp").value);

      r.freezeBefore = String(r.elements.get("freeze").textContent);
      r.elements.get("freeze").onclick();
      await new Promise((x) => setTimeout(x, 80));
      r.freezeAfter = String(r.elements.get("freeze").textContent);
      r.freezeToast = String(r.elements.get("toast").textContent || "");
    },
    check: (r) => {
      if (r.readToast.indexOf("已读取当前值") < 0) {
        console.log("     读取反馈 = " + JSON.stringify(r.readToast));
        return false;
      }
      if (r.slider !== "0.35") {
        console.log("     滑杆没被填上，当前值 = " + JSON.stringify(r.slider));
        return false;
      }
      if (r.freezeAfter === r.freezeBefore) {
        console.log("     冻结按钮没变化：" + r.freezeBefore);
        return false;
      }
      if (r.freezeToast.indexOf("冻结") < 0 && r.freezeToast.indexOf("解冻") < 0) {
        console.log("     冻结没有反馈 = " + JSON.stringify(r.freezeToast));
        return false;
      }
      return true;
    }
  },
  {
    name: "「一键改成」发的是情绪原型锚点：点「亲昵」不能变成「感动」",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      await new Promise((x) => setTimeout(x, 60));
      const html = String(r.elements.get("mood-presets").innerHTML || "");
      r.moodHTML = html;
      r.moodCount = (html.match(/data-mood=/g) || []).length;
      const idx = PROTO_TYPES.findIndex((p) => p.name === "亲昵");
      r.elements.get("mood-presets").onclick({
        target: { closest: () => ({ dataset: { mood: String(idx) } }) }
      });
      await new Promise((x) => setTimeout(x, 60));
    },
    check: (r) => {
      if (r.moodCount !== PROTO_TYPES.length) {
        console.log("     按钮数 " + r.moodCount + " ≠ 情绪原型数 " + PROTO_TYPES.length);
        return false;
      }
      if (r.moodHTML.indexOf("🥰") < 0 || r.moodHTML.indexOf("🤗") < 0) {
        console.log("     按钮上没带 emoji：" + String(r.moodHTML).slice(0, 240));
        return false;
      }
      const posts = r.bridgeCalls.filter(
        (c) => c.method === "POST" && String(c.endpoint).endsWith("/emotion/set"));
      if (!posts.length) { console.log("     点了没发 emotion/set"); return false; }
      const body = posts[posts.length - 1].body || {};
      const want = PROTO_TYPES.find((p) => p.name === "亲昵");
      if (body.p !== want.p || body.a !== want.a || body.d !== want.d) {
        console.log("     发出去的锚点=" + JSON.stringify(body) + " 期望=" + JSON.stringify(want));
        return false;
      }
      return true;
    }
  },
  {
    name: "手动添加勾了「通用」→ 提交时带 scope=global",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      r.elements.get("tab-memory").onclick();
      await new Promise((x) => setTimeout(x, 40));
      r.el("maddglobal").checked = true;
      r.el("madd").value = "他喜欢喝冰美式";
      r.el("maddbtn").onclick();
      await new Promise((x) => setTimeout(x, 40));
    },
    check: (r) => {
      const posts = r.bridgeCalls.filter(
        (c) => c.method === "POST" && String(c.endpoint).endsWith("/memory/add"));
      if (posts.length !== 1) { console.log("     memory/add 调用数 " + posts.length); return false; }
      const body = posts[0].body || {};
      if (body.scope !== "global") {
        console.log("     scope=" + JSON.stringify(body.scope));
        return false;
      }
      return body.session === "s1";
    }
  },
  {
    name: "图谱「全选」和「清空选择」能用（字段名没写错）",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      r.elements.get("tab-graph").onclick();
      await new Promise((x) => setTimeout(x, 40));
      r.elements.get("gp-selall").onclick();
      r.probeAll = String(r.elements.get("gp-selcount").textContent || "");
      r.probeActions = r.elements.get("gp-actions").style.display;
      r.elements.get("gp-selnone").onclick();
      r.probeNone = String(r.elements.get("gp-selcount").textContent || "");
      r.probeActionsNone = r.elements.get("gp-actions").style.display;
    },
    check: (r) => {
      if (r.probeAll.indexOf("已选") < 0) { console.log("     全选后：" + r.probeAll); return false; }
      if (r.probeActions === "none") return false;
      if (r.probeNone.indexOf("已选") >= 0) { console.log("     清空后：" + r.probeNone); return false; }
      if (r.probeActionsNone !== "none") return false;
      return true;
    }
  },
  {
    name: "列表复选框点一下就能选中，全选本页也能选中",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      r.elements.get("tab-memory").onclick();
      await new Promise((x) => setTimeout(x, 40));
      const boxes = r.queryAll("#mlist .memsel");
      if (!boxes.length) throw new Error("列表里解析不出复选框");

      // ① 点一个
      boxes[0].checked = true;
      r.elements.get("mlist").onchange({ target: boxes[0] });
      r.one = String(r.elements.get("mselinfo").textContent || "");
      r.oneCount = Object.keys(r.state.selected).length;

      // ② 全选本页
      const all = r.elements.get("mselectall");
      all.checked = true;
      all.onchange();
      r.all = String(r.elements.get("mselinfo").textContent || "");
      r.allCount = Object.keys(r.state.selected).length;

      // ③ 取消一个 → 计数要跟着降
      boxes[0].checked = false;
      r.elements.get("mlist").onchange({ target: boxes[0] });
      r.after = String(r.elements.get("mselinfo").textContent || "");
    },
    check: (r) => {
      if (r.one.indexOf("已选中 1 条") < 0) { console.log("     点一个后：" + r.one); return false; }
      if (r.oneCount !== 1) { console.log("     state.selected=" + r.oneCount); return false; }
      if (r.all.indexOf("已选中 2 条") < 0) { console.log("     全选本页后：" + r.all); return false; }
      if (r.allCount !== 2) { console.log("     全选后 selected=" + r.allCount); return false; }
      if (r.after.indexOf("已选中 1 条") < 0) { console.log("     取消后：" + r.after); return false; }
      return true;
    }
  },
  {
    name: "图谱「删除选中」要连点两次（确认只留一处）",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      r.elements.get("tab-graph").onclick();
      await new Promise((x) => setTimeout(x, 40));
      r.elements.get("gp-selall").onclick();
      r.elements.get("gp-del").onclick();           // 第一次：只弹提示
      r.firstToast = String(r.elements.get("toast").textContent || "");
      r.elements.get("gp-del").onclick();           // 第二次：真删
      await new Promise((x) => setTimeout(x, 40));
    },
    check: (r) => {
      const posts = r.bridgeCalls.filter(
        (c) => c.method === "POST" && String(c.endpoint).endsWith("/memory/batch"));
      if (posts.length !== 1) {
        console.log("     memory/batch 调用数 " + posts.length + "（应为 1）");
        return false;
      }
      if (r.firstToast.indexOf("确认") < 0) {
        console.log("     第一次点击没有提示：" + r.firstToast);
        return false;
      }
      return (posts[0].body || {}).action === "delete";
    }
  },
  {
    name: "强度滑块松手就提交新的强度模型，右上角显示的也是它",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      await new Promise((x) => setTimeout(x, 60));
      r.curLevel = String(r.elements.get("cur-level").textContent || "");
      r.el("si").value = "0.62";
      r.el("si").oninput();                 // 拖动中：本地读数先跟着走
      r.sliderRead = String(r.el("siv").textContent || "");
      r.el("si").onchange();                // 松手：才提交到后端
      await new Promise((x) => setTimeout(x, 60));
      r.intensityToast = String(r.elements.get("toast").textContent || "");
    },
    check: (r) => {
      const posts = r.bridgeCalls.filter(
        (c) => c.method === "POST" && String(c.endpoint).endsWith("/emotion/intensity")
      );
      if (posts.length !== 1) {
        console.log("     emotion/intensity 调用数=" + posts.length);
        return false;
      }
      const body = posts[0].body || {};
      if (body.intensity !== 0.62) {
        console.log("     提交的强度=" + JSON.stringify(body.intensity));
        return false;
      }
      if (r.curLevel.indexOf("强度 40%") < 0) {
        console.log("     右上角=" + JSON.stringify(r.curLevel));
        return false;
      }
      if (r.sliderRead !== "62%") {
        console.log("     滑块读数=" + JSON.stringify(r.sliderRead));
        return false;
      }
      if (r.intensityToast.indexOf("62%") < 0) {
        console.log("     调节反馈=" + JSON.stringify(r.intensityToast));
        return false;
      }
      return true;
    }
  },
  {
    name: "顶栏的会话搜索框：打字过滤、清空恢复，且不顶掉当前选中",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      const box = r.elements.get("session-search");
      const sel = r.elements.get("session");
      r.sessBefore = sel.value;
      // ① 一个都命不中：提示要说清楚，当前选中也不能被藏掉
      box.value = "zzz-没有这个会话";
      if (box.oninput) box.oninput();
      r.titleMiss = String(box.title || "");
      r.sessMiss = sel.value;
      // ② 命中：计数要对
      box.value = "私聊";
      if (box.oninput) box.oninput();
      r.titleHit = String(box.title || "");
      // ③ 清空（下拉框自带的 ✕ 同理）：恢复成全量的那句提示
      box.value = "";
      if (box.oninput) box.oninput();
      r.titleClear = String(box.title || "");
      r.sessClear = sel.value;
    },
    check: (r) => {
      if (typeof r.titleMiss !== "string" || r.titleMiss.indexOf("匹配 0 /") < 0) {
        console.log("     命不中时的提示=" + JSON.stringify(r.titleMiss));
        return false;
      }
      if (r.sessMiss !== r.sessBefore) {
        console.log("     过滤把当前选中顶掉了：" + r.sessBefore + " → " + r.sessMiss);
        return false;
      }
      if (r.titleHit.indexOf("匹配 1 /") < 0) {
        console.log("     命中时的提示=" + JSON.stringify(r.titleHit));
        return false;
      }
      if (r.titleClear.indexOf("匹配") >= 0) {
        console.log("     清空后提示没恢复=" + JSON.stringify(r.titleClear));
        return false;
      }
      return r.sessClear === r.sessBefore;
    }
  },
  {
    name: "备份与迁移：导出/导入各走各的接口，没给来源要给提示",
    opts: { bridge: (ep) => ep.indexOf(PLUGIN + "/") === 0 },
    after: async (r) => {
      // ① 导出
      r.elements.get("bt-export").onclick();
      await new Promise((x) => setTimeout(x, 60));
      r.exportCalls = r.bridgeCalls.filter((c) => String(c.endpoint).endsWith("transfer/export"));
      r.exportResult = String(r.elements.get("bt-result").textContent || "");
      // ② 没选文件也没填文件名：要说清楚，不能闷声不响
      r.elements.get("bt-import").onclick();
      await new Promise((x) => setTimeout(x, 40));
      r.emptyToast = String(r.elements.get("toast").textContent || "");
      r.emptyResult = String(r.elements.get("bt-result").textContent || "");
      // ③ 按文件名导入（搬家：文件已经拷进 exports/ 了）
      r.elements.get("bt-path").value = "ai-mind-backup-20260101-120000.json";
      r.elements.get("bt-import").onclick();
      await new Promise((x) => setTimeout(x, 60));
      r.importCalls = r.bridgeCalls.filter((c) => String(c.endpoint).endsWith("transfer/import"));
      r.importResult = String(r.elements.get("bt-result").textContent || "");
      // ④ 没导出过就点「下载到本地」：要给出下一步（沙箱里也不许抛异常）
      r.elements.get("bt-download").onclick();
      await new Promise((x) => setTimeout(x, 30));
      r.downloadToast = String(r.elements.get("toast").textContent || "");
    },
    check: (r) => {
      if (r.exportCalls.length !== 1 || r.exportCalls[0].method !== "POST") {
        console.log("     导出调用=" + JSON.stringify(r.exportCalls));
        return false;
      }
      if (r.exportResult.indexOf("导出完成") < 0) {
        console.log("     导出结果行=" + JSON.stringify(r.exportResult));
        return false;
      }
      if (r.emptyToast.indexOf("文件") < 0 || r.emptyResult.indexOf("没拿到备份") < 0) {
        console.log("     没给来源时：toast=" + JSON.stringify(r.emptyToast)
          + " 结果行=" + JSON.stringify(r.emptyResult));
        return false;
      }
      if (r.importCalls.length !== 1) {
        console.log("     导入调用次数=" + r.importCalls.length);
        return false;
      }
      const body = r.importCalls[0].body || {};
      if (body.path !== "ai-mind-backup-20260101-120000.json") {
        console.log("     导入 body=" + JSON.stringify(body));
        return false;
      }
      if (body.mode !== "merge" || body.confirm !== false) {
        console.log("     默认该是不带清空的合并导入：" + JSON.stringify(body));
        return false;
      }
      if (r.importResult.indexOf("导入") < 0) {
        console.log("     导入结果行=" + JSON.stringify(r.importResult));
        return false;
      }
      if (r.downloadToast.indexOf("导出备份") < 0) {
        console.log("     没内容时点下载的提示=" + JSON.stringify(r.downloadToast));
        return false;
      }
      return true;
    }
  },
];

let failed = 0;
for (const item of cases) {
  let result;
  try {
    result = await run(item.opts);
    if (item.after) await item.after(result);
  } catch (err) {
    console.log("❌ " + item.name + " —— 脚本抛异常：" + err.message);
    failed += 1;
    continue;
  }
  const ok = item.check(result);
  console.log((ok ? "✅ " : "❌ ") + item.name);
  if (!ok) {
    failed += 1;
    console.log("     status = " + JSON.stringify(result.status));
    console.log("     diag   = " + JSON.stringify(result.diag));
    console.log("     toast  = " + JSON.stringify(result.toast));
    console.log("     bridge = " + JSON.stringify(result.bridgeCalls.map((c) => c.endpoint)));
    console.log("     fetch  = " + JSON.stringify(result.fetched));
  }
}

console.log("");
console.log(failed === 0 ? "面板传输层：" + cases.length + " 项全部通过" : "面板传输层：失败 " + failed + " 项");
process.exit(failed === 0 ? 0 : 1);

