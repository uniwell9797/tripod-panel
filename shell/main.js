// 三脚架 · 独立窗口（Electron 壳）
// 用自家图标开一个原生窗口显示面板界面 —— 不经过浏览器、不弹终端。
// 面板（pythonw panel.py, 8899）没在跑的话，这里会先把它静默拉起来。
// ⚠ 路径全部用 __dirname 推出来，不留中文常量（启动器被中文编码坑过一次）。
// ⚠ 自愈：如果被以 ELECTRON_RUN_AS_NODE 跑起来（那是"纯 node"模式：窗口永远不出现，
//   而且 require("electron") 只返回一个字符串路径），就用干净环境把自己重新拉起来。
// ⚠ 2026-10-03：用户报"点了打不开"。原因就是下面这个顺序问题 —— 一次启动要是被
//   单实例锁挡掉（残留实例还在），就**连一行日志都不留、窗口永远不出现**。
//   所以：最先把"启动尝试"写进日志，并且**残留实例收到 second-instance 时若无窗口就重建窗口**。
try {
  require("fs").appendFileSync(require("path").join(__dirname, "shell.log"),
    new Date().toISOString() + "  boot attempt  runAsNode=" + (process.env.ELECTRON_RUN_AS_NODE || "") + "\n");
} catch (e) {}
(function selfHeal() {
  let api = null;
  try { api = require("electron"); } catch (e) {}
  if (api && typeof api === "object" && api.app) return;   // 正常 Electron，继续往下走
  try {
    const cp = require("child_process");
    const env = Object.assign({}, process.env);
    delete env.ELECTRON_RUN_AS_NODE;
    cp.spawn(process.execPath, [__dirname], { env: env, detached: true, stdio: "ignore" }).unref();
  } catch (e) {}
  process.exit(0);
})();

const { app, BrowserWindow, Menu, shell, ipcMain } = require("electron");
const path = require("path");
const fs = require("fs");
const http = require("http");
const { spawn, spawnSync } = require("child_process");

const SHELL_DIR = __dirname;                        // ...\shell
const HOME = path.dirname(SHELL_DIR);               // 项目根目录
const LOG = path.join(SHELL_DIR, "shell.log");
const URL_UI = "http://127.0.0.1:8899/new";
const PANEL = path.join(HOME, "panel.py");
const ICON = path.join(HOME, "logo_256.ico");

// ---- 跑面板用哪个解释器：panel_config.json 的 comfy_python > 环境变量 > 自动探测（不写死任何路径）----
function detectPython() {
  const names = process.platform === "win32" ? ["pythonw.exe", "python.exe"] : ["python3", "python"];
  for (const r of [HOME, path.dirname(HOME)]) {
    for (const venv of [".venv", "venv", "env"]) {
      for (const n of names) {
        const p = path.join(r, venv, process.platform === "win32" ? "Scripts" : "bin", n);
        try { if (fs.existsSync(p)) return p; } catch (e) {}
      }
    }
  }
  return process.platform === "win32" ? "pythonw.exe" : "python3";
}
function configPython() {
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(HOME, "panel_config.json"), "utf8"));
    if (cfg && cfg.comfy_python && fs.existsSync(cfg.comfy_python)) return cfg.comfy_python;
  } catch (e) {}
  return "";
}
const PY = process.env.PANEL_PYTHON || configPython() || detectPython();

function log(m) {
  try { fs.appendFileSync(LOG, new Date().toISOString() + "  " + m + "\n"); } catch (e) {}
}
process.on("uncaughtException", (e) => log("UNCAUGHT " + ((e && e.stack) || e)));
process.on("unhandledRejection", (e) => log("REJECT " + e));
log("==== shell start ====  __dirname=" + SHELL_DIR);

// 万一被以 ELECTRON_RUN_AS_NODE 启动（会变成纯 node、窗口永远不出现）——记一笔，方便排查
if (process.env.ELECTRON_RUN_AS_NODE) log("WARN: ELECTRON_RUN_AS_NODE=" + process.env.ELECTRON_RUN_AS_NODE);

try { app.setPath("userData", path.join(SHELL_DIR, "data")); log("userData ok"); }
catch (e) { log("setPath failed " + e); }
try { app.setName("三脚架"); } catch (e) {}

let win = null;

function ping(cb) {
  const req = http.get({ host: "127.0.0.1", port: 8899, path: "/api/settings", timeout: 1500 },
    (res) => { res.resume(); cb(res.statusCode === 200); });
  req.on("error", () => cb(false));
  req.on("timeout", () => { req.destroy(); cb(false); });
}

function startPanel() {
  try {
    const p = spawn(PY, ["-u", PANEL], { cwd: HOME, detached: true, stdio: "ignore", windowsHide: true });
    p.unref();
    log("panel spawned pid=" + p.pid);
    return true;
  } catch (e) { log("panel spawn failed " + e); return false; }
}

function waitPanel(tries, cb) {
  ping((ok) => {
    if (ok) return cb(true);
    if (tries <= 0) return cb(false);
    setTimeout(() => waitPanel(tries - 1, cb), 700);
  });
}

// 已经有面板进程在起（哪怕卡着没响应）就别再 spawn 一个 —— 多个实例互抢 8899，
// 最后谁都绑不上，用户看到的就是"启动不了"。
// （2026-10-03 真实事故：几个面板进程堆在一起、1.5 万条 TIME_WAIT，8899 一个都没绑上）
function panelProcessCount() {
  try {
    const r = spawnSync("powershell", ["-NoProfile", "-NonInteractive", "-Command",
      "@(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*panel.py*' }).Count"],
      { timeout: 8000, windowsHide: true, encoding: "utf8" });
    return parseInt((r.stdout || "0").trim(), 10) || 0;
  } catch (e) { return 0; }
}

function infoPage(msg, sub) {
  const ico = "file:///" + ICON.replace(/\\/g, "/");
  return "data:text/html;charset=utf-8," + encodeURIComponent(
    "<html><head><meta charset='utf-8'><style>body{margin:0;background:#12161c;color:#c9d3e0;" +
    "font:14px/1.9 'Microsoft YaHei',sans-serif;display:flex;flex-direction:column;align-items:center;" +
    "justify-content:center;height:100vh}img{width:88px;height:88px;border-radius:20px}" +
    "b{font-size:17px;margin-top:16px;color:#e7eefc}p{color:#7f8ea3;margin:6px 0 0}</style></head><body>" +
    "<img src='" + ico + "' onerror=\"this.style.display='none'\"><b>" + msg + "</b><p>" + (sub || "") + "</p></body></html>");
}

function createWindow() {
  log("createWindow");
  try { Menu.setApplicationMenu(null); } catch (e) { log("menu failed " + e); }
  win = new BrowserWindow({
    width: 1520, height: 960, minWidth: 1100, minHeight: 680,
    title: "TRIPOD.tec",
    icon: fs.existsSync(ICON) ? ICON : undefined,
    autoHideMenuBar: true,
    frame: false,                                  // 无边框：标题栏我们自己画（文字才能是蓝的）
    backgroundColor: "#12161c",
    show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false,
                      preload: path.join(SHELL_DIR, "preload.js") },
  });
  log("BrowserWindow ok icon=" + (fs.existsSync(ICON) ? "yes" : "no"));
  win.once("ready-to-show", () => { log("ready-to-show"); try { win.show(); } catch (e) {} });
  win.webContents.on("did-finish-load", () => { log("loaded " + win.webContents.getURL().slice(0, 60)); try{ win.setTitle("TRIPOD.tec"); }catch(e){} });
  win.webContents.on("did-fail-load", (e, c, d) => log("fail-load " + c + " " + d));
  // ⚠ 2026-10-03：谁也别想把**整个窗口**带走。内嵌的原生 ComfyUI 里若有链接用 target=_top
  //   （或者它自己跳转），窗口就会被导航到别的地址，用户就"回不到三脚架主画面"了。
  //   只允许面板自己的地址（含 hash 路由），其余一律拦下并记日志。
  win.webContents.on("will-navigate", (e, url) => {
    const u = String(url || "");
    if (!u.startsWith("http://127.0.0.1:8899")) {
      e.preventDefault();
      log("blocked navigation to " + u.slice(0, 90));
    }
  });
  try { win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: "deny" }; }); } catch (e) {}

  win.loadURL(infoPage("三脚架 正在启动…", "第一次启动要等几秒（面板 + ComfyUI）"));
  ping((ok) => {
    log("panel ping: " + ok);
    if (ok) { win.loadURL(URL_UI); return; }
    if (panelProcessCount() === 0) startPanel();
    else log("panel process already exists, not spawning another");
    waitPanel(60, (up) => {
      log("panel up: " + up);
      if (up) { win.loadURL(URL_UI); return; }
      // 没等到也别把窗口丢在错误页上：后台继续等，面板一起来就自动进界面
      win.loadURL(infoPage("三脚架 正在启动…", "面板还没响应，已在后台继续等，起来会自动进界面"));
      let n = 0;
      const iv = setInterval(() => {
        ping((ok) => {
          if (ok) { clearInterval(iv); log("panel late up, loading UI"); win.loadURL(URL_UI); }
        });
        if (++n > 60) { clearInterval(iv); log("gave up waiting for panel"); }
      }, 3000);
    });
  });
}

if (!app.requestSingleInstanceLock()) {
  log("another instance is running, quit（这次启动没有窗口，交给已有的那个实例显示）");
  app.quit();
} else {
  // ⚠ 关键：残留实例如果**没有窗口**（窗口被关了但进程没退干净），这里必须把窗口重建出来，
  //   否则用户再点几次都是"没反应"——2026-10-03 的"打不开"就是这个。
  app.on("second-instance", () => {
    log("second-instance: 再点了一次");
    try {
      if (!win || win.isDestroyed()) { log("second-instance: 没有窗口 → 重建"); createWindow(); return; }
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    } catch (e) { log("second-instance 处理失败 " + e); }
  });
  app.whenReady().then(() => { log("app ready"); createWindow(); });
  // 窗口全关了就直接退出进程：以前只 app.quit()，一旦没退干净就留下"无窗口的僵尸实例"，
  // 之后用户点多少次都只会命中单实例锁 → 静默无反应
  app.on("window-all-closed", () => { log("all closed, exit"); app.exit(0); });
  // 自绘标题栏的三个按钮
  ipcMain.on("win:min", () => { try { win && win.minimize(); } catch (e) {} });
  ipcMain.on("win:max", () => { try { if (!win) return; win.isMaximized() ? win.unmaximize() : win.maximize(); } catch (e) {} });
  ipcMain.on("win:close", () => { try { win && win.close(); } catch (e) {} });
}
