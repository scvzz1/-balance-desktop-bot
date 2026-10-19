// 仅使用调用方创建的独立配置；凭证通过父进程管道返回。
const fs = require('fs');
const path = require('path');
const {spawn} = require('child_process');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
    const base = new URL(process.argv[2]);
    if (!['https:', 'http:'].includes(base.protocol) || base.username || base.password) throw Error();
    const profile = process.argv[3];
    const browser = process.argv[4];
    const portFile = path.join(profile, 'DevToolsActivePort');
    // 记住的配置可能留有旧端口；不得连接上一次的调试会话。
    fs.rmSync(portFile, {force:true});
    const child = spawn(browser, ['--user-data-dir=' + profile, '--remote-debugging-port=0',
        '--remote-debugging-address=127.0.0.1', '--no-first-run', '--no-default-browser-check',
        '--new-window', base.href], {stdio:'ignore'});
    let exited = false;
    child.on('exit', () => { exited = true; });
    child.on('error', () => { exited = true; });
    const deadline = Date.now() + 180000;
    let socket;
    let sequence = 0;
    const pending = new Map();
    function disconnected() {
        for (const item of pending.values()) {
            clearTimeout(item.timer);
            item.reject(Error());
        }
        pending.clear();
    }
    function rpc(method, params = {}, sessionId, timeout = 8000) {
        return new Promise((resolve, reject) => {
            if (!socket || socket.readyState !== WebSocket.OPEN) return reject(Error());
            const id = ++sequence;
            const timer = setTimeout(() => { pending.delete(id); reject(Error()); }, timeout);
            pending.set(id, {resolve, reject, timer});
            try {
                socket.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
            } catch {
                clearTimeout(timer);
                pending.delete(id);
                reject(Error());
            }
        });
    }
    const expression = `(${async function(expectedOrigin) {
        if (location.origin !== expectedOrigin) return {};
        let user = {};
        let accessToken = '';
        let refreshToken = '';
        try {
            try { user = JSON.parse(localStorage.getItem('user') || '{}') || {}; } catch {}
            accessToken = localStorage.getItem('auth_token') || '';
            refreshToken = localStorage.getItem('refresh_token') || '';
        } catch {}
        const userId = /^\d+$/.test(String(user.id)) && Number(user.id) > 0 ? String(user.id) : '';
        if (typeof accessToken === 'string' && accessToken.trim()) {
            return {origin:location.origin, access_token:accessToken, refresh_token:refreshToken};
        }
        const userToken = user.access_token || user.token;
        if (typeof userToken === 'string' && userToken.trim()) {
            return {origin:location.origin, access_token:userToken, refresh_token:refreshToken,
                flavor:'newapi', ...(userId ? {user_id:userId} : {})};
        }
        async function self(headers) {
            try {
                const response = await fetch('/api/user/self', {credentials:'same-origin',
                    redirect:'error', cache:'no-store', headers, signal:AbortSignal.timeout(2500)});
                if (!response.ok || new URL(response.url).origin !== expectedOrigin) return null;
                const body = await response.json();
                const id = body && body.data && body.data.id;
                if (body.success === true && /^\d+$/.test(String(id)) && Number(id) > 0) return String(id);
            } catch {}
            return null;
        }
        let verifiedId = await self({});
        if (!verifiedId && userId) verifiedId = await self({'New-Api-User':userId});
        if (verifiedId && location.origin === expectedOrigin) {
            return {origin:location.origin, user_id:verifiedId, flavor:'newapi'};
        }
        return {};
    }.toString()})(${JSON.stringify(base.origin)})`;
    try {
        while (!fs.existsSync(portFile)) {
            if (exited || Date.now() > deadline) throw Error();
            await sleep(200);
        }
        const [port, endpoint] = fs.readFileSync(portFile, 'utf8').trim().split(/\r?\n/);
        if (!/^\d+$/.test(port) || !endpoint.startsWith('/devtools/browser/')) throw Error();
        socket = new WebSocket('ws://127.0.0.1:' + Number(port) + endpoint);
        await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(Error()), 5000);
            socket.onopen = () => { clearTimeout(timer); resolve(); };
            socket.onerror = () => { clearTimeout(timer); reject(Error()); };
        });
        socket.onclose = disconnected;
        socket.onmessage = event => {
            let message;
            try { message = JSON.parse(event.data); } catch { return; }
            const item = pending.get(message.id);
            if (!item) return;
            clearTimeout(item.timer);
            pending.delete(message.id);
            if (message.error) item.reject(Error()); else item.resolve(message.result);
        };
        while (Date.now() < deadline && !exited) {
            const {targetInfos} = await rpc('Target.getTargets');
            if (!targetInfos.some(target => target.type === 'page')) throw Error();
            for (const target of targetInfos) {
                let url;
                try { url = new URL(target.url); } catch { continue; }
                if (target.type !== 'page' || url.origin !== base.origin) continue;
                let sessionId;
                try {
                    ({sessionId} = await rpc('Target.attachToTarget', {targetId:target.targetId, flatten:true}));
                    const result = await rpc('Runtime.evaluate', {expression,
                        returnByValue:true, awaitPromise:true}, sessionId);
                    const data = result.result.value;
                    if (!data || data.origin !== base.origin) continue;
                    if (typeof data.access_token === 'string' && data.access_token.trim()) {
                        return data;
                    }
                    if (data.flavor === 'newapi' && data.user_id) {
                        const {cookies} = await rpc('Storage.getCookies', target.browserContextId
                            ? {browserContextId:target.browserContextId} : {});
                        const api = new URL('/api/user/self', base.origin);
                        const cookie = cookies.filter(item => {
                            const domain = item.domain.replace(/^\./, '').toLowerCase();
                            const host = api.hostname.toLowerCase();
                            const domainMatch = item.domain.startsWith('.')
                                ? host === domain || host.endsWith('.' + domain) : host === domain;
                            const cookiePath = item.path || '/';
                            const pathMatch = api.pathname === cookiePath || (api.pathname.startsWith(cookiePath)
                                && (cookiePath.endsWith('/') || api.pathname[cookiePath.length] === '/'));
                            const partitionMatch = !item.partitionKey || item.partitionKey.topLevelSite === base.origin;
                            return domainMatch && pathMatch && partitionMatch && !item.partitionKeyOpaque
                                && (!item.secure || api.protocol === 'https:')
                                && (!item.expires || item.expires < 0 || item.expires > Date.now() / 1000)
                                && !/[\r\n;]/.test(item.name) && !/[\r\n;]/.test(item.value);
                        }).sort((left, right) => right.path.length - left.path.length)
                            .map(item => item.name + '=' + item.value).join('; ');
                        if (cookie) return {...data, cookie};
                    }
                } catch {
                    // 登录跳转会销毁执行上下文，继续等待同源页面。
                } finally {
                    if (sessionId) await rpc('Target.detachFromTarget', {sessionId}).catch(() => {});
                }
            }
            await sleep(700);
        }
        throw Error();
    } finally {
        if (socket && socket.readyState === WebSocket.OPEN) {
            await rpc('Browser.close', {}, undefined, 2000).catch(() => {});
            socket.close();
        }
        const closeDeadline = Date.now() + 5000;
        while (!exited && Date.now() < closeDeadline) await sleep(100);
        if (!exited) {
            if (process.platform === 'win32') {
                const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'],
                    {stdio:'ignore', windowsHide:true});
                await new Promise(resolve => {
                    const timer = setTimeout(resolve, 3000);
                    killer.on('exit', () => { clearTimeout(timer); resolve(); });
                    killer.on('error', () => { clearTimeout(timer); resolve(); });
                });
            } else {
                child.kill('SIGKILL');
            }
        }
        disconnected();
    }
}

main().then(data => {
    process.stdout.write(JSON.stringify(data));
}).catch(() => {
    process.stderr.write('浏览器绑定未完成，请登录后重试。');
    process.exitCode = 1;
});
