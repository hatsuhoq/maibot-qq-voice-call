import { createHash, timingSafeEqual } from "node:crypto";
// Derived from ClaudiaGardner/maibot-qq-voice-call, GPL-3.0-only.
// Windows integration adds a short-lived MaiBot-controlled auto-answer policy.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url));
const MAX_EVENTS = 100;
// Long-poll for /v1/calls/current: the bridge checks locally every 25ms so MaiBot sees phase changes at once.
const CALL_POLL_MS = 25;
const MAX_CALL_WAIT_MS = 4000;
const SENSITIVE_KEY =
  /(auth|ticket|token|sign|open_?key|d2|a2|cookie|session|credential|password|secret)/i;
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1", "localhost"]);

let logger = null;
let pluginContext = null;
let settings = null;
let controlToken = null;
let controlServer = null;
let avsdkService = null;
let kernelCore = null;
let kernelSession = null;
let listener = null;
let listenerId = null;
let activeSDKInvite = null;
let loginTimer = null;
let acceptTimer = null;
let policy = { enabled: false, allowed: [], expiresAt: 0 };

export function mayAutoAnswer(currentPolicy, callerUin, now = Date.now()) {
  return currentPolicy.enabled === true && currentPolicy.expiresAt > now &&
    typeof callerUin === "string" && currentPolicy.allowed.includes(callerUin);
}

const state = {
  startedAt: null,
  listenerRegistered: false,
  listenerError: null,
  serviceAvailable: false,
  serviceNull: null,
  serviceMethods: [],
  eventCount: 0,
  events: [],
  avHost: idleAVHost(),
  call: idleCall(),
};

function idleAVHost() {
  return {
    loginPosted: false,
    loginAttempts: 0,
    lastLoginAt: null,
    loginPaused: false,
    loginAccountPath: null,
    outputCounts: {},
    lastByCommand: {},
    avsdkLogs: [],
    kernelActionCount: 0,
    outputCount: 0,
    networkOutputCount: 0,
    inviteCallbackSeen: false,
    autoAcceptAttemptedAt: null,
    autoAcceptInviteAt: null,
    autoAcceptPostedAt: null,
    acceptOutputAt: null,
    enterRoomOutputAt: null,
    lastOutputCommand: null,
    lastError: null,
  };
}

function idleCall() {
  return {
    phase: "idle",
    inviteAt: null,
    endedAt: null,
    endReason: null,
    inviteType: null,
    callerUid: null,
    callerUin: null,
    callerName: null,
    identityResolvedAt: null,
    identityError: null,
  };
}

function integerSetting(value, fallback, name) {
  if (value === undefined || value === null || value === "") return fallback;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`${name} must be an integer TCP port`);
  }
  return parsed;
}

export function parseBridgeSettings(env = process.env, pluginDir = PLUGIN_DIR) {
  let fileConfig = {};
  const configPath = path.join(pluginDir, "bridge-config.json");
  if (fs.existsSync(configPath)) {
    const parsed = JSON.parse(fs.readFileSync(configPath, "utf8"));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("bridge-config.json must contain an object");
    }
    fileConfig = parsed;
  }
  const controlHost = env.MAIBOT_QQ_CALL_BRIDGE_HOST || fileConfig.controlHost || "127.0.0.1";
  const avHost = env.MAIBOT_QQ_CALL_AV_HOST_HOST || fileConfig.avHost || "127.0.0.1";
  if (!LOOPBACK_HOSTS.has(controlHost) || !LOOPBACK_HOSTS.has(avHost)) {
    throw new Error("bridge endpoints must use a loopback host");
  }
  return {
    controlHost,
    controlPort: integerSetting(
      env.MAIBOT_QQ_CALL_BRIDGE_PORT || fileConfig.controlPort,
      6110,
      "bridge port",
    ),
    avHost,
    avPort: integerSetting(
      env.MAIBOT_QQ_CALL_AV_HOST_PORT || fileConfig.avPort,
      6111,
      "AV host port",
    ),
    token:
      typeof env.MAIBOT_QQ_CALL_BRIDGE_TOKEN === "string"
        ? env.MAIBOT_QQ_CALL_BRIDGE_TOKEN.trim()
        : "",
    tokenFile:
      env.MAIBOT_QQ_CALL_BRIDGE_TOKEN_FILE || fileConfig.tokenFile || "",
  };
}

function loadControlToken(config) {
  const token = config.token || fs.readFileSync(config.tokenFile, "utf8").trim();
  if (Buffer.byteLength(token, "utf8") < 32) {
    throw new Error("bridge token is missing or shorter than 32 bytes");
  }
  return token;
}

function hasValidControlToken(req, expectedToken) {
  const header = req.headers.authorization ?? "";
  if (!header.startsWith("Bearer ")) return false;
  const supplied = Buffer.from(header.slice(7), "utf8");
  const expected = Buffer.from(expectedToken, "utf8");
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}

function sendJson(res, statusCode, body) {
  const encoded = Buffer.from(JSON.stringify(body));
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": encoded.byteLength,
    "Cache-Control": "no-store",
  });
  res.end(encoded);
}

async function readJsonBody(req, limit = 2 * 1024 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new Error("request body is too large");
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8");
  return text ? JSON.parse(text) : {};
}

export function summarizeValue(value, depth = 0) {
  if (value === null) return null;
  if (value === undefined) return { type: "undefined" };
  if (depth > 2) return { type: typeof value };
  if (Buffer.isBuffer(value) || value instanceof Uint8Array) {
    return { type: "binary", length: value.byteLength };
  }
  if (Array.isArray(value)) {
    return {
      type: "array",
      length: value.length,
      items: value.slice(0, 4).map((item) => summarizeValue(item, depth + 1)),
    };
  }
  if (typeof value === "object") {
    return {
      type: "object",
      keys: Object.keys(value)
        .slice(0, 40)
        .map((key) => ({
          key,
          value: SENSITIVE_KEY.test(key)
            ? "[REDACTED]"
            : summarizeValue(value[key], depth + 1),
        })),
    };
  }
  if (typeof value === "string") return { type: "string", length: value.length };
  if (typeof value === "number" || typeof value === "boolean") {
    return { type: typeof value, value };
  }
  return { type: typeof value };
}

function discoverMethods(value) {
  const methods = new Set();
  let cursor = value;
  for (let depth = 0; cursor && depth < 6; depth += 1) {
    for (const key of Reflect.ownKeys(cursor)) {
      if (typeof key !== "string" || key === "constructor") continue;
      try {
        if (typeof value[key] === "function") methods.add(key);
      } catch {
        // Native getters may throw during discovery.
      }
    }
    cursor = Object.getPrototypeOf(cursor);
  }
  return [...methods].sort();
}

function mapValue(value, key) {
  if (!value) return null;
  if (typeof value.get === "function") return value.get(key) ?? null;
  return typeof value === "object" ? value[key] ?? null : null;
}

function firstString(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

async function resolveCallerIdentity(uid, inviteAt) {
  let uin = null;
  let profile = null;
  const errors = [];
  try {
    const result = await kernelSession?.getUixConvertService?.().getUin([uid]);
    const resolved = mapValue(result?.uinInfo, uid);
    if (resolved !== null && String(resolved) !== "0") uin = String(resolved);
  } catch (error) {
    errors.push(`uin: ${error?.message ?? String(error)}`);
  }
  try {
    const profiles = await kernelCore?.eventWrapper?.callNoListenerEvent?.(
      "NodeIKernelProfileService/getCoreAndBaseInfo",
      "nodeStore",
      [uid],
    );
    profile = mapValue(profiles, uid);
    const profileUin = profile?.uin ?? profile?.coreInfo?.uin;
    if (!uin && profileUin !== null && profileUin !== undefined && String(profileUin) !== "0") {
      uin = String(profileUin);
    }
  } catch (error) {
    errors.push(`profile: ${error?.message ?? String(error)}`);
  }
  if (state.call.inviteAt !== inviteAt || state.call.callerUid !== uid) return;
  state.call = {
    ...state.call,
    callerUin: uin,
    callerName: firstString(
      profile?.remark,
      profile?.displayName,
      profile?.nick,
      profile?.nickname,
      profile?.coreInfo?.remark,
      profile?.coreInfo?.nick,
      profile?.simpleInfo?.coreInfo?.nick,
    ),
    identityResolvedAt: new Date().toISOString(),
    identityError: uin ? null : errors.join("; ") || "caller identity lookup returned empty",
  };
  if (uin && activeSDKInvite) scheduleAccept(0);
}

export function buildAcceptParams(invite) {
  if (!Array.isArray(invite) || invite.length < 12) {
    throw new Error("AVSDK invite callback is incomplete");
  }
  // Verified against QQ Linux AVSDK. invite[2] contains this account, while
  // the native Accept session key must use the inviter at invite[1].
  const params = [
    invite[0],
    invite[1],
    [invite[1]],
    invite[3],
    invite[4],
    Boolean(invite[10]),
    invite[11],
  ];
  const valid =
    typeof params[0] === "number" &&
    typeof params[1] === "string" &&
    Array.isArray(params[2]) &&
    params[2].every((item) => typeof item === "string") &&
    typeof params[3] === "number" &&
    typeof params[4] === "string" &&
    typeof params[5] === "boolean" &&
    typeof params[6] === "string";
  if (!valid) throw new Error("AVSDK invite does not match the native Accept signature");
  return params;
}

function invokeAVHost(command, params, retries = 0) {
  return new Promise((resolve, reject) => {
    const encoded = Buffer.from(JSON.stringify({ command, params }));
    const request = http.request(
      {
        host: settings.avHost,
        port: settings.avPort,
        path: "/v1/invoke",
        method: "POST",
        headers: {
          Authorization: `Bearer ${controlToken}`,
          "Content-Type": "application/json",
          "Content-Length": encoded.byteLength,
        },
        timeout: 3000,
      },
      (response) => {
        response.resume();
        response.on("end", () => {
          if (response.statusCode >= 200 && response.statusCode < 300) resolve();
          else reject(new Error(`AV host returned HTTP ${response.statusCode}`));
        });
      },
    );
    request.on("timeout", () => request.destroy(new Error("AV host timeout")));
    request.on("error", (error) => {
      if (retries > 0) {
        setTimeout(() => invokeAVHost(command, params, retries - 1).then(resolve, reject), 250);
      } else reject(error);
    });
    request.end(encoded);
  });
}

async function acceptActiveInvite() {
  if (!Array.isArray(activeSDKInvite) || state.call.phase === "ended") return;
  if (!mayAutoAnswer(policy, state.call.callerUin)) return;
  const inviteAt = state.call.inviteAt;
  if (!inviteAt || state.avHost.autoAcceptInviteAt === inviteAt) return;
  state.avHost.autoAcceptInviteAt = inviteAt;
  state.avHost.autoAcceptAttemptedAt = new Date().toISOString();
  state.call.phase = "accepting";
  await invokeAVHost(5, buildAcceptParams(activeSDKInvite));
  state.avHost.autoAcceptPostedAt = new Date().toISOString();
}

function scheduleAccept(delayMs) {
  if (acceptTimer) clearTimeout(acceptTimer);
  acceptTimer = setTimeout(() => {
    acceptTimer = null;
    void acceptActiveInvite().catch((error) => {
      state.call.phase = "ringing";
      state.avHost.lastError = `native auto-accept failed: ${error?.message ?? String(error)}`;
      logger?.warn(`[MaiBotQQCall] ${state.avHost.lastError}`);
    });
  }, delayMs);
  acceptTimer.unref?.();
}

function forwardKernelAction(name, args) {
  let actionType = null;
  let payload = null;
  if (name.toLowerCase() === "oninviteactiontoavsdk") {
    actionType = args[1];
    payload = args[2];
  } else if (name.toLowerCase() === "onactiontoavsdk") {
    actionType = args[0];
    payload = args[1];
  }
  if (typeof actionType !== "number" || typeof payload !== "string") return;
  void invokeAVHost(55, [actionType, payload]).then(
    () => {
      state.avHost.kernelActionCount += 1;
      state.avHost.lastError = null;
      if (name.toLowerCase() === "onactiontoavsdk" && activeSDKInvite) scheduleAccept(75);
    },
    (error) => {
      state.avHost.lastError = `kernel action forward failed: ${error?.message ?? String(error)}`;
    },
  );
}

function recordEvent(name, args) {
  const now = new Date().toISOString();
  const lowerName = name.toLowerCase();
  if (lowerName === "oninviteactiontoavsdk") {
    activeSDKInvite = null;
    state.avHost.autoAcceptAttemptedAt = null;
    state.avHost.autoAcceptInviteAt = null;
    state.avHost.autoAcceptPostedAt = null;
    state.call = {
      ...idleCall(),
      phase: "ringing",
      inviteAt: now,
      inviteType: typeof args[0]?.invite_type === "number" ? args[0].invite_type : null,
    };
  } else if (lowerName === "ons2cactiontoavsdk" && typeof args[0]?.destroyReason === "number") {
    if (acceptTimer) clearTimeout(acceptTimer);
    acceptTimer = null;
    activeSDKInvite = null;
    state.call = {
      ...state.call,
      phase: "ended",
      endedAt: now,
      endReason: args[0].destroyReason,
    };
  }
  state.eventCount += 1;
  state.events.push({ name, at: now, args: args.map((arg) => summarizeValue(arg)) });
  if (state.events.length > MAX_EVENTS) state.events.shift();
  forwardKernelAction(name, args);
}

function makeListener() {
  const target = {};
  for (const name of [
    "onS2CActionToAVSDK",
    "onActionToAVSDK",
    "onAVSDKData",
    "onAVSdkCrash",
    "onReceiveInvite",
    "onInviteActionToAVSDK",
    "onGroupVideoActionToAVSDK",
    "onGroupVideoServerPushToAVSDK",
  ]) {
    target[name] = (...args) => recordEvent(name, args);
  }
  return new Proxy(target, {
    get(object, property, receiver) {
      if (Reflect.has(object, property)) return Reflect.get(object, property, receiver);
      if (typeof property === "string") {
        const callback = (...args) => recordEvent(property, args);
        Reflect.set(object, property, callback);
        return callback;
      }
      return Reflect.get(object, property, receiver);
    },
  });
}

// 诊断用：保留数字、布尔值和短字符串，长字符串只记长度，敏感键名脱敏。
export function peekValue(value, depth = 0, maxString = 80) {
  if (value === null || value === undefined) return value ?? null;
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "string") return value.length <= maxString ? value : `<string ${value.length}>`;
  if (depth > 2) return `<${Array.isArray(value) ? "array" : typeof value}>`;
  if (Array.isArray(value)) return value.slice(0, 12).map((item) => peekValue(item, depth + 1, maxString));
  if (typeof value === "object") {
    const result = {};
    for (const key of Object.keys(value).slice(0, 20)) {
      result[key] = SENSITIVE_KEY.test(key) ? "[REDACTED]" : peekValue(value[key], depth + 1, maxString);
    }
    return result;
  }
  return `<${typeof value}>`;
}

const LOGIN_COOLDOWN_MS = 5000;
const MAX_RELOGINS = 5;

async function handleAVSDKOutput(body) {
  const command = body?.command;
  const value = body?.value;
  if (!Number.isInteger(command)) throw new Error("invalid AVSDK output command");
  state.avHost.outputCount += 1;
  state.avHost.lastOutputCommand = command;
  state.avHost.outputCounts[command] = (state.avHost.outputCounts[command] ?? 0) + 1;
  const at = new Date().toISOString();
  if (command === 20050) {
    // Windows AVSDK 用 20050 输出运行日志（不是“需要重新登录”）。
    const line = Array.isArray(value) && typeof value[0] === "string" ? value[0] : JSON.stringify(peekValue(value));
    state.avHost.avsdkLogs.push(`${at} ${String(line).slice(0, 400)}`);
    if (state.avHost.avsdkLogs.length > 40) state.avHost.avsdkLogs.shift();
  } else if (command !== 20001) {
    state.avHost.lastByCommand[command] = { at, value: peekValue(value, 0, 300) };
  }
  if (command === 120043) {
    // AV Host 刚启动或重启：重新计数并登录。
    state.avHost.loginAttempts = 0;
    state.avHost.loginPaused = false;
    state.avHost.loginPosted = true;
  }
  if (
    command === 120043 &&
    state.avHost.loginPosted &&
    pluginContext
  ) {
    state.avHost.loginPosted = false;
    // 带冷却和次数上限的重登，避免登录参数不对时无限循环。
    if (state.avHost.loginAttempts >= MAX_RELOGINS) {
      state.avHost.loginPaused = true;
      state.avHost.lastError = `AVSDK 连续要求重新登录 ${MAX_RELOGINS} 次，已暂停；最后一次输出 ${command}`;
    } else {
      const since = state.avHost.lastLoginAt ? Date.now() - Date.parse(state.avHost.lastLoginAt) : LOGIN_COOLDOWN_MS;
      scheduleAVHostLogin(pluginContext, Math.max(100, LOGIN_COOLDOWN_MS - since));
    }
  }
  if (command === 20001) {
    if (!Array.isArray(value) || typeof value[0] !== "number" || typeof value[1] !== "string") {
      throw new Error("invalid AVSDK network output");
    }
    if (value[1].length > 1024 * 1024) throw new Error("AVSDK network output is too large");
    if (!avsdkService || typeof avsdkService.setActionFromAVSDK !== "function") {
      throw new Error("setActionFromAVSDK is unavailable");
    }
    await avsdkService.setActionFromAVSDK(value[0], value[1]);
    state.avHost.networkOutputCount += 1;
  } else if (command === 20006 && Array.isArray(value)) {
    activeSDKInvite = value;
    const callerUid = typeof value[1] === "string" ? value[1] : null;
    state.call = { ...state.call, callerUid, callerUin: null, callerName: null };
    state.avHost.inviteCallbackSeen = true;
    if (callerUid) void resolveCallerIdentity(callerUid, state.call.inviteAt);
    scheduleAccept(500);
  } else if (command === 5) {
    state.avHost.acceptOutputAt = new Date().toISOString();
    state.call.phase = Array.isArray(value) && value[0] === 0 ? "accepted" : "ringing";
  } else if (command === 20004) {
    state.avHost.enterRoomOutputAt = new Date().toISOString();
    state.call.phase = Array.isArray(value) && value[0] === 0 ? "connected" : "ringing";
  }
  state.avHost.lastError = null;
}

export function callVersion(call) {
  return createHash("sha1").update(JSON.stringify(call)).digest("hex").slice(0, 16);
}

async function waitForCallChange(since, waitMs, isClosed) {
  const deadline = Date.now() + waitMs;
  while (!isClosed() && callVersion(state.call) === since && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, CALL_POLL_MS));
  }
}

function publicStatus() {
  return {
    version: "0.3.5",
    startedAt: state.startedAt,
    listenerRegistered: state.listenerRegistered,
    listenerError: state.listenerError,
    serviceAvailable: state.serviceAvailable,
    serviceNull: state.serviceNull,
    serviceMethods: state.serviceMethods,
    avHost: state.avHost,
    call: state.call,
    eventCount: state.eventCount,
    recentEvents: state.events.slice(-10),
  };
}

async function startControlServer() {
  settings = parseBridgeSettings();
  controlToken = loadControlToken(settings);
  controlServer = http.createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", `http://${settings.controlHost}:${settings.controlPort}`);
    if (req.method === "GET" && url.pathname === "/healthz") {
      return sendJson(res, 200, { ok: true });
    }
    if (!hasValidControlToken(req, controlToken)) {
      return sendJson(res, 401, { code: -1, message: "Unauthorized" });
    }
    if (req.method === "PUT" && url.pathname === "/v1/policy") {
      try {
        const body = await readJsonBody(req, 8192);
        if (typeof body.enabled !== "boolean" || !Array.isArray(body.allowed) || body.allowed.length > 100 ||
            !body.allowed.every(id => typeof id === "string" && /^\d{5,20}$/.test(id))) {
          throw new Error("invalid policy");
        }
        if (body.enabled && !state.listenerRegistered) throw new Error("AVSDK listener is unavailable");
        policy = {enabled: body.enabled, allowed: body.allowed, expiresAt: Date.now() + 15000};
        return sendJson(res, 200, {code: 0, data: {enabled: policy.enabled, leaseSeconds: 15}});
      } catch (_error) {
        return sendJson(res, 400, {code: -1, message: "自动接听策略无效或 AVSDK 监听器不可用"});
      }
    }
    if (req.method === "GET" && url.pathname === "/v1/status") {
      return sendJson(res, 200, { code: 0, data: publicStatus() });
    }
    if (req.method === "GET" && url.pathname === "/v1/calls/current") {
      // ?since=<version>&waitMs=<n>: hold the request until the call state changes or the wait ends.
      const since = url.searchParams.get("since");
      const waitMs = Math.min(Math.max(Number(url.searchParams.get("waitMs")) || 0, 0), MAX_CALL_WAIT_MS);
      if (since && waitMs) {
        let closed = false;
        res.on("close", () => { closed = true; });
        await waitForCallChange(since, waitMs, () => closed);
        if (closed) return;
      }
      return sendJson(res, 200, { code: 0, data: state.call, version: callVersion(state.call) });
    }
    if (req.method === "POST" && url.pathname === "/v1/avsdk/output") {
      try {
        await handleAVSDKOutput(await readJsonBody(req));
        return sendJson(res, 200, { code: 0, message: "accepted" });
      } catch (error) {
        state.avHost.lastError = error?.message ?? String(error);
        return sendJson(res, 400, { code: -1, message: "invalid AVSDK output" });
      }
    }
    return sendJson(res, 404, { code: -1, message: "Not Found" });
  });
  controlServer.on("clientError", (_error, socket) => socket.destroy());
  await new Promise((resolve, reject) => {
    controlServer.once("error", reject);
    controlServer.listen(settings.controlPort, settings.controlHost, resolve);
  });
}

function scheduleAVHostLogin(ctx, delayMs = 500) {
  if (loginTimer) clearTimeout(loginTimer);
  loginTimer = setTimeout(async () => {
    try {
      const session = ctx.core?.context?.session;
      const selfUid = String(ctx.core?.selfInfo?.uid ?? "");
      const selfUin = String(ctx.core?.selfInfo?.uin ?? "");
      const accountPath = String(
        session?.getAccountPath?.(Number.parseInt(selfUin, 10)) || ctx.core?.dataPath || "",
      );
      if (!selfUid || !selfUin || !accountPath) throw new Error("QQ identity is unavailable");
      state.avHost.loginAttempts += 1;
      state.avHost.lastLoginAt = new Date().toISOString();
      state.avHost.loginAccountPath = accountPath;
      await invokeAVHost(1, [selfUid, selfUin, selfUin, accountPath, ""]);
      state.avHost.loginPosted = true;
      state.avHost.lastError = null;
    } catch (error) {
      state.avHost.lastError = `AV host login failed: ${error?.message ?? String(error)}`;
      scheduleAVHostLogin(ctx, Math.min(delayMs * 2, 5000));
    }
  }, delayMs);
  loginTimer.unref?.();
}

export const plugin_init = async (ctx) => {
  pluginContext = ctx;
  logger = ctx.logger;
  kernelCore = ctx.core ?? null;
  kernelSession = ctx.core?.context?.session ?? null;
  Object.assign(state, {
    startedAt: new Date().toISOString(),
    listenerRegistered: false,
    listenerError: null,
    serviceAvailable: false,
    serviceNull: null,
    serviceMethods: [],
    eventCount: 0,
    events: [],
    avHost: idleAVHost(),
    call: idleCall(),
  });
  try {
    avsdkService = kernelSession?.getAVSDKService?.() ?? null;
    state.serviceAvailable = Boolean(avsdkService);
    state.serviceMethods = avsdkService ? discoverMethods(avsdkService) : [];
    state.serviceNull = avsdkService?.isNull ? Boolean(avsdkService.isNull()) : null;
    if (avsdkService?.addKernelAVSDKListener) {
      listener = makeListener();
      listenerId = avsdkService.addKernelAVSDKListener(listener);
      state.listenerRegistered = true;
    }
    await startControlServer();
    scheduleAVHostLogin(ctx);
    logger.info(
      `[MaiBotQQCall] bridge listening on ${settings.controlHost}:${settings.controlPort}`,
    );
  } catch (error) {
    state.listenerError = error?.message ?? String(error);
    logger.error(`[MaiBotQQCall] bridge startup failed: ${state.listenerError}`);
  }
  ctx.router.get("/maibot-qq-call/status", (_req, res) => {
    res.json({ code: 0, data: publicStatus() });
  });
};

export const plugin_cleanup = async () => {
  policy = {enabled: false, allowed: [], expiresAt: 0};
  pluginContext = null;
  if (loginTimer) clearTimeout(loginTimer);
  if (acceptTimer) clearTimeout(acceptTimer);
  loginTimer = null;
  acceptTimer = null;
  if (controlServer) {
    const closing = new Promise((resolve) => controlServer.close(resolve));
    controlServer.closeAllConnections?.(); // don't wait for pending long-polls
    await closing;
    controlServer = null;
  }
  if (avsdkService && listenerId !== null && avsdkService.removeKernelAVSDKListener) {
    try {
      avsdkService.removeKernelAVSDKListener(listenerId);
    } catch (error) {
      logger?.warn(`[MaiBotQQCall] listener cleanup failed: ${error?.message ?? String(error)}`);
    }
  }
  settings = null;
  controlToken = null;
  avsdkService = null;
  kernelCore = null;
  kernelSession = null;
  listener = null;
  listenerId = null;
  activeSDKInvite = null;
  state.listenerRegistered = false;
};

export const plugin_get_config = async () => ({
  controlHost: settings?.controlHost ?? "127.0.0.1",
  controlPort: settings?.controlPort ?? 6110,
  avHost: settings?.avHost ?? "127.0.0.1",
  avPort: settings?.avPort ?? 6111,
});

export const plugin_config_ui = [];
