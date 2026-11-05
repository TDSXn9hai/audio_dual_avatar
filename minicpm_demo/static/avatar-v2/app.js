import { AvatarSessionClient } from '../sdk/avatar-session-client.js?v=session-api6';


const $ = (id) => document.getElementById(id);
let client = null;
let starting = false;
let muted = false;
let currentResponse = null;
const MIC_DEVICE_KEY = 'avatar.ui.micDevice';


function setStatus(text) {
    $('status').textContent = text;
}


function setHint(text) {
    $('hint').textContent = text;
}


function updateControls() {
    const running = Boolean(client);
    $('start').disabled = starting || running;
    $('stop').disabled = !running;
    $('interrupt').disabled = !running;
    $('mute').disabled = !running;
    $('mute').textContent = muted ? '取消静音' : '静音';
    $('micDevice').disabled = starting || running;
    $('refreshDevices').disabled = starting || running;
}


function appendTranscript(text) {
    if (!text) return;
    if (!currentResponse) {
        currentResponse = document.createElement('div');
        currentResponse.className = 'message assistant';
        $('transcript').append(currentResponse);
    }
    currentResponse.textContent += text;
    $('transcript').scrollTop = $('transcript').scrollHeight;
}


async function loadPresets() {
    const data = await AvatarSessionClient.listPresets();
    $('preset').replaceChildren();
    for (const item of data.presets || []) {
        if (!item.available) continue;
        $('preset').add(new Option(item.name, item.id));
    }
    if (data.default_preset_id) $('preset').value = data.default_preset_id;
}


async function loadMicrophones({ requestPermission = false } = {}) {
    if (requestPermission) {
        const permissionStream = await navigator.mediaDevices.getUserMedia({
            audio: true,
            video: false,
        });
        for (const track of permissionStream.getTracks()) track.stop();
    }
    const saved = localStorage.getItem(MIC_DEVICE_KEY) || '';
    const current = $('micDevice').value || saved;
    const devices = await navigator.mediaDevices.enumerateDevices();
    const microphones = devices.filter((device) => device.kind === 'audioinput');
    $('micDevice').replaceChildren(new Option('系统默认麦克风', ''));
    let index = 0;
    for (const device of microphones) {
        if (!device.deviceId || device.deviceId === 'default') continue;
        index += 1;
        const label = device.label || `麦克风 ${index}`;
        $('micDevice').add(new Option(label, device.deviceId));
    }
    if (current && [...$('micDevice').options].some((option) => option.value === current)) {
        $('micDevice').value = current;
    } else if (current) {
        $('micDevice').add(new Option('已保存的麦克风（等待授权）', current));
        $('micDevice').value = current;
    }
}


async function start() {
    if (starting || client) return;
    starting = true;
    currentResponse = null;
    updateControls();
    setStatus('创建会话…');
    setHint('正在通过 /v1/avatar/sessions 创建服务端编排会话');
    try {
        const instance = new AvatarSessionClient({
            canvas: $('avatarCanvas'),
            presetId: $('preset').value,
            microphoneDeviceId: $('micDevice').value,
        });
        client = instance;
        instance.on('ready', (message) => {
            setStatus(`已连接 · ${message.video_codec} · ${message.width}×${message.height}`);
            setHint('发送麦克风音频即可开始对话');
        });
        instance.on('microphoneReady', (info) => {
            const label = info?.label ? `（${info.label}）` : '';
            setHint(`麦克风已授权${label}，正在准备数字人会话…`);
            loadMicrophones().catch(() => {});
        });
        instance.on('microphoneStarted', () => {
            setStatus('麦克风已开启 · 正在聆听');
            setHint('请对着麦克风说话，绿色音量状态会在收到声音后更新');
        });
        instance.on('microphoneLevel', ({ rms, sourceRms }) => {
            if (sourceRms > 0.01) {
                setStatus('正在收音');
            } else if (rms === 0 && sourceRms > 0.001) {
                setStatus('麦克风有输入，但采集节点输出为静音');
            }
        });
        instance.on('microphoneTrackState', ({ muted: trackMuted }) => {
            setStatus(
                trackMuted
                    ? '麦克风被系统静音'
                    : '麦克风已恢复',
            );
        });
        instance.on('audioAccepted', ({ rms }) => {
            if (Number(rms) < 0.002) {
                setStatus('麦克风无信号，请检查设备和系统权限');
            } else {
                setStatus('服务器已收到音频');
            }
        });
        instance.on('transcript', ({ text }) => appendTranscript(text));
        instance.on('responseStarted', () => {
            currentResponse = null;
        });
        instance.on('responseEnded', () => {
            currentResponse = null;
        });
        instance.on('interrupted', () => {
            currentResponse = null;
            setStatus('已打断');
        });
        instance.on('closed', ({ reason } = {}) => {
            if (client !== instance) return;
            client = null;
            starting = false;
            setStatus('已断开');
            setHint(`会话结束：${reason || 'closed'}`);
            updateControls();
        });
        instance.on('error', (error) => {
            console.error(error);
            setStatus('会话异常');
            setHint(error?.message || '会话异常');
        });
        if (!navigator.mediaDevices?.getUserMedia) {
            throw new Error('当前浏览器不能采集麦克风，请使用 HTTPS');
        }
        await instance.prepareMicrophone();
        if ($('micDevice').value) {
            localStorage.setItem(MIC_DEVICE_KEY, $('micDevice').value);
        }
        await instance.connect();
        await instance.startMicrophone();
        starting = false;
        setStatus('会话已就绪');
        updateControls();
    } catch (error) {
        console.error(error);
        client?.close();
        client = null;
        starting = false;
        setStatus('启动失败');
        setHint(error?.message || '启动失败');
        updateControls();
    }
}


$('start').addEventListener('click', start);
$('interrupt').addEventListener('click', () => client?.interrupt());
$('mute').addEventListener('click', () => {
    if (!client) return;
    muted = !muted;
    client.mute(muted);
    updateControls();
});
$('stop').addEventListener('click', () => client?.close());
$('micDevice').addEventListener('change', () => {
    localStorage.setItem(MIC_DEVICE_KEY, $('micDevice').value);
});
$('refreshDevices').addEventListener('click', () => {
    loadMicrophones({ requestPermission: true }).catch((error) => {
        setHint(error?.message || '麦克风列表刷新失败');
    });
});
window.addEventListener('pagehide', () => client?.close());


updateControls();
loadPresets().catch((error) => {
    setStatus('预设加载失败');
    setHint(error?.message || '预设加载失败');
});
loadMicrophones().catch((error) => {
    setHint(error?.message || '麦克风列表加载失败');
});
