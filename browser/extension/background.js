// A local relay transports commands; every evaluation carries an explicit tab ID.
let socket;
const jobs = new Map();
const normalPage = url => /^https?:\/\//.test(url || '');

function send(message) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

async function publishTabs(type = 'tabs_update') {
  const tabs = (await chrome.tabs.query({})).filter(tab => normalPage(tab.url));
  send({type, tabs: tabs.map(({id, title, url}) => ({id, title, url}))});
}

async function evaluate(message) {
  const {id, tabId, code} = message;
  let attached = false;
  try {
    if (!Number.isInteger(tabId)) throw new Error('Explicit tabId required');
    const command = JSON.parse(code);
    if (command.cmd !== 'cdp' || command.method !== 'Runtime.evaluate' || command.tabId !== tabId) {
      throw new Error('Unsupported command or mismatched target');
    }
    const tab = await chrome.tabs.get(tabId);
    if (!normalPage(tab.url)) throw new Error('Selected tab is not an HTTP(S) page');
    await chrome.debugger.attach({tabId}, '1.3');
    attached = true;
    const result = await chrome.debugger.sendCommand({tabId}, command.method, command.params || {});
    send({type: 'result', id, result});
  } catch (error) {
    send({type: 'error', id, error: error.message || String(error)});
  } finally {
    if (attached) {
      try { await chrome.debugger.detach({tabId}); } catch (_) {}
    }
  }
}

function connect() {
  if (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState)) return;
  socket = new WebSocket('ws://127.0.0.1:18765');
  socket.onopen = () => { publishTabs('ext_ready').catch(() => {}); };
  socket.onmessage = event => {
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (!message.id || !message.code) return;
    const previous = jobs.get(message.tabId) || Promise.resolve();
    const job = previous.catch(() => {}).then(() => evaluate(message));
    jobs.set(message.tabId, job);
    job.finally(() => { if (jobs.get(message.tabId) === job) jobs.delete(message.tabId); });
  };
  socket.onclose = () => { setTimeout(connect, 2000); };
  socket.onerror = () => { socket.close(); };
}

for (const event of [chrome.tabs.onCreated, chrome.tabs.onUpdated, chrome.tabs.onRemoved]) {
  event.addListener(() => { publishTabs().catch(() => {}); });
}
chrome.alarms.create('bridge-keepalive', {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(() => {
  connect();
  send({type: 'ping'});
});
connect();
