/**
 * AvatarPlayer — MuseTalk /ws/live client for the omni page in avatar mode.
 *
 * Ported from MuseTalk web/app.js. Each binary message is one encoded frame
 * behind a 24-byte header (magic "MTF1", kind, segment id, pts_us, frame
 * index). Each `segment` message points at a WAV that is scheduled on the
 * AudioContext at the segment's pts; the audio clock picks the frame to
 * draw, and ACKs tell the server how far playback has got.
 */

const FRAME_MAGIC = 0x4d544631;
const IDLE_SEGMENT = 0xffffffff;
const FRAME_IDLE = 0;
const FRAME_SILENT = 2;
const RETRY_MS = 3000;
export const INTERRUPT_FLUSH_TIMEOUT_MS = 2500;
// Frames of a segment needed before its audio may start (server meta.prebuffer
// is the answer-start delay, already applied on the server timeline).
const CLIENT_PREBUFFER_FRAMES = 8;
// Audio starting later than its pts by more than this shifts the video clock.
const TIMELINE_SLACK_S = 0.01;

export class AvatarPlayer {
    /**
     * @param {object} opts
     * @param {HTMLCanvasElement} opts.canvas
     * @param {HTMLElement} [opts.statusEl]   text status line
     * @param {HTMLElement} [opts.subtitleEl] subtitle line over the avatar
     * @param {string} [opts.base]            same-origin proxy prefix
     * @param {number} [opts.interruptTimeoutMs] flush recovery watchdog
     */
    constructor({
        canvas,
        statusEl = null,
        subtitleEl = null,
        base = '/avatar-live',
        interruptTimeoutMs = INTERRUPT_FLUSH_TIMEOUT_MS,
    }) {
        this.canvas = canvas;
        this.statusEl = statusEl;
        this.subtitleEl = subtitleEl;
        this.base = base;
        this.ws = null;
        this.ctx = null;
        this.gain = null;
        this.fps = 24;
        this.prebuffer = 8;
        this.maxPrefetch = 4;
        this.closed = false;
        this.frames = new Map();
        this._raf = 0;
        this._retryTimer = null;
        this._subtitleTimers = [];
        this._speaking = null;
        this.interruptTimeoutMs = interruptTimeoutMs;
        this.onInterruptAcknowledged = null;
        this.onInterruptTimeout = null;
        this._interruptTimer = null;
        this._interruptToken = 0;
        this._resetTimeline();
    }

    /** Subtitle lag ≈ server answer-start buffer + render/network margin. */
    get latencyMs() {
        return Math.round((this.prebuffer / this.fps) * 1000) + 300;
    }

    get hasPendingSpeech() {
        if (!this.ctx) return false;
        return this.segments.some((segment) => segment.startAt === null
            || segment.startAt + segment.duration > this.ctx.currentTime);
    }

    /**
     * Unlock audio and connect. Call from a user gesture (the Start click).
     * @param {(ctx: AudioContext) => void} [applySink] output device hook
     */
    async start(applySink) {
        if (!this.ctx) {
            this.ctx = new AudioContext();
            this.gain = this.ctx.createGain();
            this.gain.connect(this.ctx.destination);
            if (applySink) {
                try { applySink(this.ctx); } catch (_) { /* keep default sink */ }
            }
        }
        if (this.ctx.state === 'suspended') await this.ctx.resume();
        this._connect();
        if (!this._raf) this._raf = requestAnimationFrame(() => this._render());
    }

    close() {
        this.closed = true;
        this._clearInterruptWatchdog();
        clearTimeout(this._retryTimer);
        if (this._raf) cancelAnimationFrame(this._raf);
        this._raf = 0;
        this.clearSubtitle();
        if (this.ws) this.ws.close();
        this._stopAudio();
    }

    /** Stop queued playback immediately; the next forced-listen chunk flushes the server. */
    interrupt() {
        this._discardUntilFlush = true;
        this._dropFromCurrent();
        this.clearSubtitle();
        this._speaking = false;
        this._setStatus('idle', '正在听你说');
        this._startInterruptWatchdog();
    }

    /** Show model text after the avatar latency so it lines up with the mouth. */
    setSubtitle(text) {
        this._later(() => this._showSubtitle(text));
    }

    endSubtitle() {
        this._later(() => {
            const t = setTimeout(() => this._showSubtitle(''), 1500);
            this._subtitleTimers.push(t);
        });
    }

    clearSubtitle() {
        for (const t of this._subtitleTimers) clearTimeout(t);
        this._subtitleTimers = [];
        this._showSubtitle('');
    }

    _later(fn) {
        const t = setTimeout(() => {
            this._subtitleTimers = this._subtitleTimers.filter((x) => x !== t);
            fn();
        }, this.latencyMs);
        this._subtitleTimers.push(t);
    }

    _showSubtitle(text) {
        if (!this.subtitleEl) return;
        // Turn text accumulates; keep the latest part on one or two lines.
        const shown = text && text.length > 48 ? `…${text.slice(-48)}` : text;
        this.subtitleEl.textContent = shown;
        this.subtitleEl.hidden = !shown;
    }

    _setStatus(state, text) {
        if (!this.statusEl) return;
        this.statusEl.textContent = text;
        this.statusEl.dataset.state = state;
    }

    // ---- connection ----

    _connect() {
        if (this.closed || (this.ws && this.ws.readyState <= WebSocket.OPEN)) return;
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const ws = new WebSocket(`${proto}//${location.host}${this.base}/ws/live`);
        ws.binaryType = 'arraybuffer';
        this.ws = ws;
        this._setStatus('connecting', '数字人连接中…');
        ws.onopen = () => {
            // Server frame indices restart at 0 for every viewer connection.
            this._resetTimeline();
            this.epoch = this.ctx.currentTime + 0.12;
            ws.send(JSON.stringify({ type: 'ready' }));
        };
        ws.onmessage = (event) => {
            if (typeof event.data === 'string') this._onControl(JSON.parse(event.data));
            else this._onFrame(event.data);
        };
        ws.onclose = () => {
            if (this.ws === ws) this.ws = null;
            this._stopAudio();
            if (this.closed) return;
            if (this.statusEl && this.statusEl.dataset.state !== 'error') {
                this._setStatus('error', '数字人连接断开，正在重连…');
            }
            clearTimeout(this._retryTimer);
            this._retryTimer = setTimeout(() => this._connect(), RETRY_MS);
        };
    }

    _onControl(msg) {
        switch (msg.type) {
        case 'meta':
            this.fps = msg.fps || 24;
            this.prebuffer = msg.prebuffer || 8;
            this.maxPrefetch = msg.max_prefetch_seconds || 4;
            if (msg.width && msg.height) {
                this.canvas.width = msg.width;
                this.canvas.height = msg.height;
            }
            this._speaking = null;
            this._setStatus('idle', '数字人待机');
            break;
        case 'segment': {
            if (this._discardUntilFlush) break;
            const segment = {
                id: msg.id,
                totalFrames: Number(msg.total_frames || 0),
                startFrame: Number(msg.start_frame || 0),
                startPtsUs: Number(msg.start_pts_us || 0),
                duration: Number(msg.duration || 0),
                audioUrl: `${this.base}${msg.audio_url}`,
                audioBuffer: null,
                receivedFrames: new Set(),
                startAt: null,
                source: null,
            };
            this.segments.push(segment);
            this._loadAudio(segment);
            break;
        }
        case 'flush':
            this._discardUntilFlush = false;
            this._clearInterruptWatchdog();
            // Upstream barge-in: drop the current answer, nothing resumes.
            this._dropFromCurrent();
            this.clearSubtitle();
            // The server restarts its timeline right after this frame.
            if (Number.isFinite(msg.frame_index)) {
                this.lastDrawn = Math.min(this.lastDrawn, msg.frame_index);
                for (const [index, frame] of this.frames) {
                    if (index > msg.frame_index) {
                        frame.bitmap.close();
                        this.frames.delete(index);
                    }
                }
            }
            if (typeof this.onInterruptAcknowledged === 'function') {
                this.onInterruptAcknowledged();
            }
            break;
        case 'error':
        case 'live_end':
            this._setStatus('error', msg.message || '数字人服务异常');
            break;
        default:
            break;
        }
    }

    // ---- media ----

    _onFrame(buffer) {
        if (this._discardUntilFlush) return;
        if (buffer.byteLength <= 24) return;
        const view = new DataView(buffer);
        if (view.getUint32(0) !== FRAME_MAGIC) return;
        const kind = view.getUint8(4);
        const segmentId = view.getUint32(8);
        const ptsUs = Number(view.getBigInt64(12));
        const index = view.getUint32(20);
        const ws = this.ws;
        const pixels = new Uint8Array(buffer, 24);
        const webp = pixels[0] === 0x52 && pixels[1] === 0x49
            && pixels[8] === 0x57 && pixels[9] === 0x45;
        createImageBitmap(new Blob([pixels], { type: webp ? 'image/webp' : 'image/jpeg' }))
            .then((bitmap) => {
                if (this.ws !== ws) { bitmap.close(); return; }
                const old = this.frames.get(index);
                if (old) old.bitmap.close();
                this.frames.set(index, { bitmap, kind, segmentId, ptsUs });
                if (segmentId !== IDLE_SEGMENT) {
                    const segment = this.segments.find((s) => s.id === segmentId);
                    if (segment) segment.receivedFrames.add(index);
                }
                if (this.lastDrawn >= 0) this._prune(this.lastDrawn);
                this._scheduleReady();
            })
            .catch(() => {});
    }

    _clearInterruptWatchdog() {
        if (this._interruptTimer !== null) {
            clearTimeout(this._interruptTimer);
            this._interruptTimer = null;
        }
        this._interruptToken += 1;
    }

    _startInterruptWatchdog() {
        this._clearInterruptWatchdog();
        const token = this._interruptToken;
        const timeoutMs = Math.max(250, Number(this.interruptTimeoutMs) || INTERRUPT_FLUSH_TIMEOUT_MS);
        this._interruptTimer = setTimeout(() => {
            this._interruptTimer = null;
            if (token !== this._interruptToken || !this._discardUntilFlush) return;
            console.warn(`[AvatarPlayer] no flush after ${timeoutMs}ms; recovering viewer`);
            this._discardUntilFlush = false;
            this._resetTimeline();
            this._setStatus('error', '数字人未收到打断确认，正在重连…');
            if (typeof this.onInterruptTimeout === 'function') this.onInterruptTimeout();
            if (this.ws) {
                try { this.ws.close(); } catch (_) { /* reconnect below */ }
            } else if (!this.closed) {
                this._connect();
            }
        }, timeoutMs);
    }

    async _loadAudio(segment) {
        try {
            const resp = await fetch(segment.audioUrl, { cache: 'no-store' });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.arrayBuffer();
            if (!this.segments.includes(segment)) return;
            segment.audioBuffer = await this.ctx.decodeAudioData(data);
        } catch (err) {
            // Play the frames silently rather than blocking the video forever.
            console.warn(`[AvatarPlayer] segment ${segment.id} audio failed:`, err);
            segment.audioFailed = true;
        }
        this._scheduleReady();
    }

    _hasPrebuffer(segment) {
        const needed = Math.min(segment.totalFrames, CLIENT_PREBUFFER_FRAMES);
        for (let i = 0; i < needed; i += 1) {
            if (!segment.receivedFrames.has(segment.startFrame + i)) return false;
        }
        return true;
    }

    /**
     * Audio is the master clock. A segment's frames are held back until its
     * audio is scheduled; if the audio has to start later than the segment's
     * pts (late WAV, frames still arriving, previous audio still playing),
     * the whole video timeline shifts by that delay so lips stay on the voice.
     */
    _scheduleReady() {
        if (!this.ctx) return;
        while (this.scheduleCursor < this.segments.length) {
            const segment = this.segments[this.scheduleCursor];
            const audioReady = segment.audioBuffer || segment.audioFailed;
            if (!audioReady || !this._hasPrebuffer(segment)) break;
            const due = this.epoch + segment.startPtsUs / 1e6;
            const when = Math.max(this.ctx.currentTime + 0.08, due, this.audioScheduleEnd);
            if (when - due > TIMELINE_SLACK_S) {
                this.epoch += when - due;
                console.log(`[AvatarPlayer] segment ${segment.id}: video timeline shifted +${((when - due) * 1000).toFixed(0)}ms to follow audio`);
            }
            segment.startAt = when;
            this.scheduleCursor += 1;
            if (!segment.audioBuffer) {
                this.audioScheduleEnd = when + segment.duration;
                continue;
            }
            const source = this.ctx.createBufferSource();
            source.buffer = segment.audioBuffer;
            source.connect(this.gain);
            source.start(when);
            segment.source = source;
            this.audioScheduleEnd = when + segment.audioBuffer.duration;
        }
    }

    // ---- playback clock ----

    _render() {
        this._raf = requestAnimationFrame(() => this._render());
        if (!this.ctx || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        const now = this.ctx.currentTime;
        while (this.currentIndex + 1 < this.segments.length) {
            const next = this.segments[this.currentIndex + 1];
            if (next.startAt === null || now < next.startAt) break;
            this._release(this.segments[this.currentIndex]);
            this.currentIndex += 1;
        }
        this._trimSegments();

        const target = Math.floor(Math.max(0, now - this.epoch) * this.fps);
        this._drawAt(target);
        this._ack(this.lastDrawn);

        const segment = this.segments[this.currentIndex];
        const speaking = Boolean(
            segment && segment.startAt !== null
            && now >= segment.startAt && now < segment.startAt + segment.duration,
        );
        if (speaking !== this._speaking) {
            this._speaking = speaking;
            this._setStatus(speaking ? 'speaking' : 'idle', speaking ? '数字人说话中' : '数字人待机');
        }
    }

    _drawAt(target) {
        // Hold the picture before the first segment whose audio is not
        // scheduled yet; it starts together with its voice.
        const pending = this.segments[this.scheduleCursor];
        const limit = pending ? Math.min(target, pending.startFrame - 1) : target;
        let selectedIndex = -1;
        let selected = null;
        for (const [index, frame] of this.frames) {
            if (index <= limit && index > selectedIndex) {
                selectedIndex = index;
                selected = frame;
            }
        }
        if (!selected || selectedIndex <= this.lastDrawn) return;
        this.canvas.getContext('2d').drawImage(
            selected.bitmap, 0, 0, this.canvas.width, this.canvas.height,
        );
        this.lastDrawn = selectedIndex;
        this._prune(selectedIndex);
    }

    _prune(anchor) {
        const minIndex = Math.max(0, anchor - 6);
        for (const [index, frame] of this.frames) {
            if (index < minIndex) {
                frame.bitmap.close();
                this.frames.delete(index);
            }
        }
    }

    _ack(index) {
        const frame = this.frames.get(index);
        if (!frame || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        const segment = frame.segmentId === IDLE_SEGMENT
            ? null
            : this.segments.find((s) => s.id === frame.segmentId);
        const finalMedia = segment && frame.kind !== FRAME_IDLE
            && index >= segment.startFrame + segment.totalFrames - 1;
        let send = false;
        if (frame.kind !== FRAME_IDLE && index > this.lastMediaAck
            && (index - this.lastMediaAck >= 6 || finalMedia)) {
            this.lastMediaAck = index;
            send = true;
        }
        if (index - this.lastAck >= 6) {
            this.lastAck = index;
            send = true;
        }
        if (!send) return;
        const kind = frame.kind === FRAME_IDLE ? 'idle'
            : frame.kind === FRAME_SILENT ? 'silent' : 'speech';
        this.ws.send(JSON.stringify({
            type: 'ack', frame_index: index, pts_us: frame.ptsUs, kind,
        }));
    }

    // ---- bookkeeping ----

    _dropFromCurrent() {
        for (let i = this.currentIndex; i < this.segments.length; i += 1) {
            const segment = this.segments[i];
            this._stopSource(segment);
            for (const index of segment.receivedFrames) {
                const frame = this.frames.get(index);
                if (frame) {
                    frame.bitmap.close();
                    this.frames.delete(index);
                }
            }
            this._release(segment);
        }
        this.segments = this.segments.slice(0, this.currentIndex);
        this.scheduleCursor = this.segments.length;
        this.currentIndex = this.segments.length;
        this.audioScheduleEnd = 0;
    }

    _stopSource(segment) {
        if (!segment.source) return;
        try { segment.source.stop(); } catch (_) { /* already ended */ }
        segment.source = null;
    }

    _stopAudio() {
        for (const segment of this.segments) this._stopSource(segment);
    }

    _release(segment) {
        segment.audioBuffer = null;
        segment.source = null;
    }

    /** Forget segments that finished long ago so the list stays short. */
    _trimSegments() {
        const drop = this.currentIndex - 8;
        if (drop <= 0) return;
        this.segments.splice(0, drop);
        this.currentIndex -= drop;
        this.scheduleCursor -= drop;
    }

    _resetTimeline() {
        this._discardUntilFlush = false;
        this._clearInterruptWatchdog();
        for (const frame of this.frames.values()) frame.bitmap.close();
        this.frames = new Map();
        this.segments = [];
        this.scheduleCursor = 0;
        this.currentIndex = 0;
        this.audioScheduleEnd = 0;
        this.epoch = 0;
        this.lastDrawn = -1;
        this.lastAck = -1;
        this.lastMediaAck = -1;
    }
}
