// Only the separately launched AV host uses this loader; the running QQ is not changed.
// 第二个 QQ 进程的标准输出看不到，所以把启动过程和异常写进 bridge/runtime/av-host.log。
"use strict";
const fs = require("node:fs");
const path = require("node:path");

// NapCat 注入器把本文件当作 QQ 目录里的 loadNapCat.js 执行，__dirname 指向 QQ，不能用来找插件文件。
const bridgeDir = process.env.MAIBOT_QQ_CALL_BRIDGE_DIR || path.join(__dirname, "..");
const runtimeDir = path.join(bridgeDir, "runtime");
const logFile = path.join(runtimeDir, "av-host.log");

function avLog(message) {
  try {
    fs.mkdirSync(runtimeDir, { recursive: true });
    fs.appendFileSync(logFile, `[${new Date().toISOString()}] ${message}\n`);
  } catch {
    // 日志失败不能影响通话进程。
  }
}

globalThis.maibotAVHostLog = avLog;
process.on("uncaughtException", (error) => {
  avLog(`uncaughtException: ${error?.stack ?? String(error)}`);
  try { require("electron").app.exit(1); } catch { process.exit(1); }
});
process.on("unhandledRejection", (error) => avLog(`unhandledRejection: ${error?.stack ?? String(error)}`));

avLog(`loader start pid=${process.pid} electron=${process.versions.electron} node=${process.versions.node}`);
avLog(`env avsdk=${process.env.MAIBOT_QQ_CALL_AVSDK_PATH} token=${process.env.MAIBOT_QQ_CALL_BRIDGE_TOKEN_FILE} ` +
  `ports=${process.env.MAIBOT_QQ_CALL_BRIDGE_PORT}/${process.env.MAIBOT_QQ_CALL_AV_HOST_PORT}`);
try {
  require(path.join(bridgeDir, "av-host", "host.cjs"));
  avLog("host.cjs loaded");
} catch (error) {
  avLog(`host.cjs failed: ${error?.stack ?? String(error)}`);
  throw error;
}
