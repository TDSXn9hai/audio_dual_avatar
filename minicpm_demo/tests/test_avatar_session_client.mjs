import assert from 'node:assert/strict';
import fs from 'node:fs/promises';


const source = await fs.readFile(
    new URL('../static/sdk/avatar-session-client.js', import.meta.url),
    'utf8',
);
const events = [];
let requestedConstraints = null;

globalThis.AudioContext = class {
    constructor(options = {}) {
        this.sampleRate = options.sampleRate || 48000;
        this.destination = {};
        this.audioWorklet = {
            addModule: async () => {
                events.push(`module:${this.sampleRate}`);
            },
        };
    }
    async resume() {
        events.push(`resume:${this.sampleRate}`);
    }
    async close() {}
    createGain() {
        return { connect() {}, gain: { value: 1 } };
    }
    createMediaStreamSource() {
        return { connect() {}, disconnect() {} };
    }
    createMediaStreamDestination() {
        return {};
    }
    createAnalyser() {
        return {
            fftSize: 256,
            connect() {},
            disconnect() {},
            getFloatTimeDomainData(target) {
                target.fill(0);
            },
        };
    }
};
globalThis.AudioWorkletNode = class {
    constructor() {
        this.port = {
            postMessage: (message) => events.push(`worklet:${message.command}`),
        };
    }
    connect() {}
    disconnect() {}
};
Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {
        mediaDevices: {
            getUserMedia: async (constraints) => {
                events.push('getUserMedia');
                requestedConstraints = constraints;
                return {
                    getTracks: () => [{
                        stop() {},
                        addEventListener() {},
                    }],
                    getAudioTracks: () => [{
                        enabled: true,
                        addEventListener() {},
                    }],
                };
            },
        },
    },
});
globalThis.location = { origin: 'https://example.test' };

const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const { AvatarSessionClient } = await import(moduleUrl);
const client = new AvatarSessionClient({
    baseUrl: 'https://example.test',
    canvas: {},
    microphoneDeviceId: 'mic-2',
});
let ready = false;
client.on('microphoneReady', () => {
    ready = true;
});

await client.prepareMicrophone();
assert.equal(ready, true);
assert.equal(events.includes('getUserMedia'), true);
assert.equal(
    requestedConstraints.audio.deviceId.exact,
    'mic-2',
    'selected microphone must be sent as an exact getUserMedia constraint',
);
assert.equal(
    events.findIndex((value) => value === 'resume:48000')
        < events.findIndex((value) => value === 'getUserMedia'),
    true,
    'playback AudioContext must be unlocked in the user gesture',
);
assert.equal(
    events.some((value) => value === 'worklet:start'),
    false,
    'capture must not start before the session is ready',
);

await client.startMicrophone();
assert.equal(events.includes('worklet:start'), true);
client.close();

console.log('AvatarSessionClient microphone preparation checks passed');
