import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import net from "node:net";
import test from "node:test";

import {
  buildAcceptParams,
  parseBridgeSettings,
  plugin_cleanup,
  plugin_init,
  summarizeValue,
} from "../napcat-plugin/index.mjs";

test("verified native Accept mapping uses the inviter as the session key", () => {
  const invite = [
    1,
    "inviter-uid",
    ["bot-uid"],
    3,
    "relation-id",
    5,
    6,
    7,
    8,
    9,
    1,
    "denoise-model",
  ];
  assert.deepEqual(buildAcceptParams(invite), [
    1,
    "inviter-uid",
    ["inviter-uid"],
    3,
    "relation-id",
    true,
    "denoise-model",
  ]);
});

test("invalid invite shape is rejected before invoking AVSDK", () => {
  assert.throws(() => buildAcceptParams([1, "uid"]), /incomplete/);
  const invite = Array(12).fill(null);
  assert.throws(() => buildAcceptParams(invite), /native Accept signature/);
});

test("diagnostic summaries redact sensitive fields and omit string contents", () => {
  const summary = summarizeValue({
    accessToken: "do-not-log-this",
    callerName: "Alice",
    nested: { cookie: "also-secret" },
  });
  const serialized = JSON.stringify(summary);
  assert.doesNotMatch(serialized, /do-not-log-this|also-secret|Alice/);
  assert.equal(summary.keys.find((item) => item.key === "accessToken").value, "[REDACTED]");
});

test("bridge settings load generated config and allow environment port overrides", () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "maibot-qq-call-"));
  try {
    fs.writeFileSync(
      path.join(tempDir, "bridge-config.json"),
      JSON.stringify({
        controlHost: "127.0.0.1",
        controlPort: 6110,
        avHost: "localhost",
        avPort: 6111,
        tokenFile: "/run/private/token",
      }),
    );
    const settings = parseBridgeSettings(
      { MAIBOT_QQ_CALL_BRIDGE_PORT: "6210" },
      tempDir,
    );
    assert.equal(settings.controlPort, 6210);
    assert.equal(settings.avHost, "localhost");
    assert.equal(settings.tokenFile, "/run/private/token");
    assert.equal(settings.token, "");
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
});

test("bridge refuses non-loopback endpoints", () => {
  assert.throws(
    () => parseBridgeSettings({ MAIBOT_QQ_CALL_BRIDGE_HOST: "0.0.0.0" }, os.tmpdir()),
    /loopback/,
  );
});

async function unusedLoopbackPort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const selectedPort = address.port;
  await new Promise((resolve) => server.close(resolve));
  return selectedPort;
}

test("NapCat lifecycle exposes only authenticated call state", async () => {
  const controlPort = await unusedLoopbackPort();
  const avPort = await unusedLoopbackPort();
  const previous = {
    token: process.env.MAIBOT_QQ_CALL_BRIDGE_TOKEN,
    tokenFile: process.env.MAIBOT_QQ_CALL_BRIDGE_TOKEN_FILE,
    controlPort: process.env.MAIBOT_QQ_CALL_BRIDGE_PORT,
    avPort: process.env.MAIBOT_QQ_CALL_AV_HOST_PORT,
  };
  const token = "test-token-that-is-longer-than-thirty-two-bytes";
  process.env.MAIBOT_QQ_CALL_BRIDGE_TOKEN = token;
  delete process.env.MAIBOT_QQ_CALL_BRIDGE_TOKEN_FILE;
  process.env.MAIBOT_QQ_CALL_BRIDGE_PORT = String(controlPort);
  process.env.MAIBOT_QQ_CALL_AV_HOST_PORT = String(avPort);

  let registeredListener = null;
  const service = {
    isNull: () => false,
    addKernelAVSDKListener(value) {
      registeredListener = value;
      return 7;
    },
    removeKernelAVSDKListener(id) {
      assert.equal(id, 7);
      registeredListener = null;
    },
  };
  const context = {
    logger: { info() {}, warn() {}, error() {} },
    router: { get() {} },
    core: {
      selfInfo: {},
      context: { session: { getAVSDKService: () => service } },
    },
  };

  try {
    await plugin_init(context);
    assert.ok(registeredListener);
    const baseUrl = `http://127.0.0.1:${controlPort}`;
    const health = await fetch(`${baseUrl}/healthz`);
    assert.equal(health.status, 200);
    const unauthorized = await fetch(`${baseUrl}/v1/calls/current`);
    assert.equal(unauthorized.status, 401);
    const authorized = await fetch(`${baseUrl}/v1/calls/current`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    assert.equal(authorized.status, 200);
    const payload = await authorized.json();
    assert.equal(payload.data.phase, "idle");
    assert.equal(typeof payload.version, "string");
    const started = Date.now();
    const held = await fetch(`${baseUrl}/v1/calls/current?since=${payload.version}&waitMs=300`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    assert.ok(Date.now() - started >= 250, "unchanged state should be held until waitMs");
    assert.equal((await held.json()).version, payload.version);
    const changed = await fetch(`${baseUrl}/v1/calls/current?since=stale&waitMs=3000`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    assert.equal((await changed.json()).version, payload.version);
  } finally {
    await plugin_cleanup();
    for (const [key, value] of Object.entries(previous)) {
      const envName = {
        token: "MAIBOT_QQ_CALL_BRIDGE_TOKEN",
        tokenFile: "MAIBOT_QQ_CALL_BRIDGE_TOKEN_FILE",
        controlPort: "MAIBOT_QQ_CALL_BRIDGE_PORT",
        avPort: "MAIBOT_QQ_CALL_AV_HOST_PORT",
      }[key];
      if (value === undefined) delete process.env[envName];
      else process.env[envName] = value;
    }
  }
  assert.equal(registeredListener, null);
});
