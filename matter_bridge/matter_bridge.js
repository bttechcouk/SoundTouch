/**
 * SoundTouch Matter Bridge
 *
 * Exposes SoundTouch speaker presets and power toggles as Matter On/Off devices
 * so Amazon Echo (5th gen) can control them via the Matter protocol.
 *
 * Each speaker contributes:
 *   - Up to 6 preset slots (named after stored preset, e.g. "Living Room - BBC Radio 4")
 *   - 1 power toggle (e.g. "Living Room - Power")
 *   - 1 dimmable volume device (e.g. "Living Room - Volume", level 0-254 → volume 0-100)
 */

import "@project-chip/matter-node.js"; // registers Node.js crypto/net/time

import { MatterServer, CommissioningServer } from "@project-chip/matter-node.js";
import { StorageManager, StorageBackendJsonFile } from "@project-chip/matter-node.js/storage";
import { Logger, Level, Format } from "@project-chip/matter-node.js/log";
import { Aggregator, OnOffPluginUnitDevice, DimmablePluginUnitDevice } from "@project-chip/matter.js/device";
import { QrCode, QrPairingCodeCodec, ManualPairingCodeCodec } from "@project-chip/matter.js/schema";
import { VendorId } from "@project-chip/matter.js/datatype";

import http from "http";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const PYTHON_API_BASE = "http://localhost:8888";
const BASE_DIR = path.dirname(fileURLToPath(import.meta.url));
const DATA_DIR = path.join(BASE_DIR, "data", "matter");
const LOG_FILE = path.join(BASE_DIR, "matter_bridge.log");
const MATTER_PORT = 5540;
const BRIDGE_API_PORT = 8889; // local HTTP API (QR code for web UI)
const VENDOR_ID = 0xfff1; // test vendor (use during dev/commissioning)
const PRODUCT_ID = 0x8001;
const DEVICE_NAME = "SoundTouch Bridge";

// Device label format — controls how Alexa names each preset/power device.
// Available tokens: {preset} = preset name, {room} = speaker name
// "Alexa, turn on {label}" — so pick a format that sounds natural after "turn on":
//   "{preset} in {room}"  →  "Alexa, turn on KISS in Bedroom"
//   "{room} {preset}"     →  "Alexa, turn on Bedroom KISS"
//   "{room} - {preset}"   →  "Alexa, turn on Bedroom - KISS"  (original)
const LABEL_FORMAT  = "{preset} in {room} Bose";
const POWER_FORMAT  = "{room} Bose power";
const VOLUME_FORMAT = "{room} Bose volume";
const PASSCODE = 20202021;
const DISCRIMINATOR = 3840;

// ---------------------------------------------------------------------------
// Logging — console + rotating log file
// ---------------------------------------------------------------------------

Logger.defaultLogLevel = Level.DEBUG;
Logger.format = Format.PLAIN;

// Tee logs to a file alongside journal output
const logStream = fs.createWriteStream(LOG_FILE, { flags: "a" });
const origLog = console.log.bind(console);
console.log = (...args) => {
    const line = args.join(" ");
    origLog(line);
    logStream.write(line + "\n");
};

const logger = Logger.get("SoundTouchBridge");

// ---------------------------------------------------------------------------
// HTTP helper — calls the Python SoundTouch controller REST API
// ---------------------------------------------------------------------------

function apiGet(path) {
    return new Promise((resolve, reject) => {
        const url = `${PYTHON_API_BASE}${path}`;
        http.get(url, (res) => {
            let data = "";
            res.on("data", (chunk) => (data += chunk));
            res.on("end", () => {
                try {
                    resolve(JSON.parse(data));
                } catch {
                    reject(new Error(`JSON parse error from ${url}: ${data.slice(0, 200)}`));
                }
            });
        }).on("error", reject);
    });
}

// ---------------------------------------------------------------------------
// Discover speakers + their presets via the Python API
// ---------------------------------------------------------------------------

async function waitForSpeakers(maxWaitSecs = 120) {
    const deadline = Date.now() + maxWaitSecs * 1000;
    let attempt = 0;
    while (Date.now() < deadline) {
        attempt++;
        try {
            const speakers = await apiGet("/api/speakers");
            if (Array.isArray(speakers) && speakers.length > 0) {
                logger.info(`Python API ready — ${speakers.length} speaker(s) found (attempt ${attempt})`);
                return speakers;
            }
            logger.info(`Waiting for speakers (attempt ${attempt}) — API returned empty list, retrying in 5s…`);
        } catch (err) {
            logger.info(`Waiting for Python API (attempt ${attempt}): ${err.message} — retrying in 5s…`);
        }
        await new Promise(r => setTimeout(r, 5000));
    }
    throw new Error(`Python API did not return speakers within ${maxWaitSecs}s — aborting`);
}

async function discoverDevices() {
    logger.info("Querying Python API for speakers…");
    const speakers = await waitForSpeakers();

    const devices = [];

    for (const speaker of speakers) {
        const { host, name: speakerName } = speaker;
        logger.info(`  Speaker: ${speakerName} (${host})`);

        let state;
        try {
            state = await apiGet(`/api/state?host=${encodeURIComponent(host)}`);
        } catch (err) {
            logger.warn(`  Could not get state for ${host}: ${err.message}`);
            continue;
        }

        const presets = state.presets ?? [];

        // Preset devices (slots 1-6)
        for (let i = 1; i <= 6; i++) {
            const preset = presets.find((p) => Number(p.id) === i);
            const presetName = preset?.name?.trim() || `Preset ${i}`;
            const deviceLabel = LABEL_FORMAT
                .replace("{preset}", presetName)
                .replace("{room}", speakerName);

            devices.push({
                label: deviceLabel,
                action: () => apiGet(`/api/cmd?host=${encodeURIComponent(host)}&action=preset${i}`),
            });
        }

        // Power toggle device
        devices.push({
            label: POWER_FORMAT.replace("{room}", speakerName),
            action: () => apiGet(`/api/cmd?host=${encodeURIComponent(host)}&action=power`),
        });

        // Volume device — DimmablePluginUnit, level 0-254 maps to volume 0-100
        const initialVolume = state.volume ?? 0;
        devices.push({
            label: VOLUME_FORMAT.replace("{room}", speakerName),
            buildDevice: () => buildVolumeDevice(speakerName, host, initialVolume),
        });

        // Zone join device — adds this speaker to the current group
        devices.push({
            label: `Join Group ${speakerName}`,
            action: () => apiGet(`/api/group/join?host=${encodeURIComponent(host)}`),
        });
    }

    // Global zone devices (one each, not per-speaker)
    devices.push({
        label: "Party Mode",
        action: () => apiGet("/api/group/party"),
    });
    devices.push({
        label: "Dissolve Group",
        action: () => apiGet("/api/group/dissolve-all"),
    });

    logger.info(`Discovered ${devices.length} Matter devices total`);
    return devices;
}

// ---------------------------------------------------------------------------
// Build Matter devices from logical device list
// ---------------------------------------------------------------------------

function buildMatterDevice(label, action, endpointIndex) {
    const device = new OnOffPluginUnitDevice();

    // When Alexa (or any controller) turns the device "on", fire the action.
    // We immediately reset to off since presets/power are momentary actions,
    // not persistent toggle states.
    device.addOnOffListener(async (newValue, oldValue) => {
        if (newValue === true && oldValue === false) {
            logger.info(`[${label}] → triggered`);
            try {
                await action();
                logger.info(`[${label}] ✓ command sent`);
            } catch (err) {
                logger.error(`[${label}] ✗ command failed: ${err.message}`);
            }
            // Reset state back to off (preset/power are momentary)
            setTimeout(() => device.setOnOff(false), 500);
        }
    });

    return device;
}

function buildVolumeDevice(speakerName, host, initialVolume) {
    const initialLevel = Math.round(initialVolume / 100 * 254);
    const device = new DimmablePluginUnitDevice(
        { onOff: true },
        { currentLevel: initialLevel, minLevel: 0, maxLevel: 254 },
    );

    let syncing = false;

    device.addCurrentLevelListener(async (newLevel, oldLevel) => {
        if (syncing || newLevel === null || newLevel === oldLevel) return;
        const volume = Math.round(newLevel / 254 * 100);
        logger.info(`[${speakerName} volume] → ${volume}% (level ${newLevel})`);
        try {
            await apiGet(`/api/cmd?host=${encodeURIComponent(host)}&action=volume&value=${volume}`);
            logger.info(`[${speakerName} volume] ✓ volume set to ${volume}`);
        } catch (err) {
            logger.error(`[${speakerName} volume] ✗ command failed: ${err.message}`);
        }
    });

    // Keep level in sync when volume is changed from the web UI
    const SYNC_INTERVAL_MS = 10_000;
    const syncTimer = setInterval(async () => {
        try {
            const state = await apiGet(`/api/state?host=${encodeURIComponent(host)}`);
            const level = Math.round((state.volume ?? 0) / 100 * 254);
            if (level !== device.getCurrentLevel()) {
                syncing = true;
                device.setCurrentLevel(level);
                setTimeout(() => { syncing = false; }, 200);
            }
        } catch { /* ignore — speaker may be offline */ }
    }, SYNC_INTERVAL_MS);

    // Clean up timer on process exit
    process.on("exit", () => clearInterval(syncTimer));

    return device;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
    // Ensure data directory exists
    fs.mkdirSync(DATA_DIR, { recursive: true });

    // Storage
    const storage = new StorageBackendJsonFile(path.join(DATA_DIR, "bridge.json"));
    const storageManager = new StorageManager(storage);
    await storageManager.initialize();

    // Matter server
    const matterServer = new MatterServer(storageManager);

    // Discover logical devices — waits until Python API has speakers
    const logicalDevices = await discoverDevices();

    // Build aggregator (Matter bridge endpoint)
    const aggregator = new Aggregator();

    for (let i = 0; i < logicalDevices.length; i++) {
        const { label, action, buildDevice } = logicalDevices[i];
        const matterDevice = buildDevice ? buildDevice() : buildMatterDevice(label, action, i);
        const shortLabel = label.length > 32 ? label.slice(0, 32) : label;
        aggregator.addBridgedDevice(matterDevice, {
            nodeLabel: shortLabel,
            productName: "SoundTouch",
            productLabel: shortLabel,
            serialNumber: `st-${i}`,
            reachable: true,
        });
        logger.info(`  Registered: [${i}] ${label}`);
    }

    // Commissioning server
    const commissioningServer = new CommissioningServer({
        port: MATTER_PORT,
        deviceName: DEVICE_NAME,
        deviceType: 0x000e, // Aggregator
        passcode: PASSCODE,
        discriminator: DISCRIMINATOR,
        basicInformation: {
            vendorId: VendorId(VENDOR_ID),
            vendorName: "SoundTouch",
            productId: PRODUCT_ID,
            productName: "SoundTouch Bridge",
            nodeLabel: DEVICE_NAME,
            hardwareVersion: 1,
            softwareVersion: 1,
        },
    });

    commissioningServer.addDevice(aggregator);
    matterServer.addCommissioningServer(commissioningServer);

    await matterServer.start();

    // Log commissioning state
    const commissioned = commissioningServer.isCommissioned();
    logger.info(`Commissioning state: ${commissioned ? "ALREADY COMMISSIONED" : "NOT YET COMMISSIONED — pairing required"}`);

    // Print commissioning info
    const { qrPairingCode, manualPairingCode } = commissioningServer.getPairingCode();

    logger.info("=".repeat(60));
    logger.info("Matter bridge running — commission with Alexa app:");
    logger.info(`  Manual pairing code : ${manualPairingCode}`);
    logger.info(`  QR pairing code     : ${qrPairingCode}`);
    logger.info("");

    // Render QR code to console
    let qrText = null;
    try {
        qrText = QrCode.get(qrPairingCode);
        logger.info("Scan this QR code in the Alexa app (Add Device → Other → Matter):");
        console.log(qrText);
    } catch {
        // QrCode rendering not available — manual code is enough
    }

    logger.info("=".repeat(60));

    // Local HTTP API — serves pairing info to the web UI on localhost
    const bridgeApiServer = http.createServer((req, res) => {
        res.setHeader("Access-Control-Allow-Origin", "*");
        if (req.url === "/qr") {
            res.writeHead(200, { "Content-Type": "application/json" });
            // Check commissioned state dynamically so it reflects post-startup commissioning
            res.end(JSON.stringify({
                qrPairingCode,
                manualPairingCode,
                commissioned: commissioningServer.isCommissioned(),
                qrText,
            }));
        } else {
            res.writeHead(404);
            res.end("Not found");
        }
    });
    bridgeApiServer.listen(BRIDGE_API_PORT, "127.0.0.1", () => {
        logger.info(`Bridge API listening on localhost:${BRIDGE_API_PORT}`);
    });

    // Graceful shutdown
    process.on("SIGINT", async () => {
        logger.info("Shutting down Matter bridge…");
        await matterServer.close();
        process.exit(0);
    });
    process.on("SIGTERM", async () => {
        logger.info("Shutting down Matter bridge…");
        await matterServer.close();
        process.exit(0);
    });
}

main().catch((err) => {
    console.error("Fatal error:", err);
    process.exit(1);
});
