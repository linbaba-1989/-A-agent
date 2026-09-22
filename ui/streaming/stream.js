// No price generation, interpolation, or client-side freshness policy.
const numeric = value => typeof value === 'number' && Number.isFinite(value);
export function priceDirection(before, after, previousSeq, nextSeq) {
  if (nextSeq <= previousSeq || !numeric(before) || !numeric(after)) return '';
  return after > before ? 'up' : after < before ? 'down' : '';
}
export function rowDelta(before, after) {
  if (before && after.snapshot_seq <= before.snapshot_seq) return {fields: [], flash: ''};
  return {fields: Object.keys(after).filter(key => !before || before[key] !== after[key]),
    flash: before ? priceDirection(before.lastPrice, after.lastPrice, before.snapshot_seq, after.snapshot_seq) : ''};
}
const display = (value, key) => {
  if (value == null || value === 'unavailable') return '--';
  if (!numeric(value)) return String(value).replace('T', ' ');
  if (key === 'amount') return value >= 1e8 ? (value / 1e8).toFixed(2) + '亿' : (value / 1e4).toFixed(2) + '万';
  return value.toFixed(2) + (['change_pct', 'speed_1m', 'speed_3m', 'speed_5m'].includes(key) ? '%' : '');
};
const heroFields = [['lastPrice','最新价'],['change','涨跌额'],['change_pct','涨跌幅'],['amount','成交额'],
  ['speed_1m','1m'],['speed_3m','3m'],['speed_5m','5m'],['quote_time','单股行情时间']];
const rankFields = ['rank','symbol','name','lastPrice','change_pct','speed_1m','speed_3m','speed_5m','amount'];
export function createRenderer(doc, {acceptanceMode=false} = {}) {
  const get = id => doc.getElementById(id);
  const hero = new Map(), rows = new Map();
  let target = null, seq = -1, updates = 0, lastRenderMs = 0;
  function text(node, value) { if (node.textContent !== value) node.textContent = value; }
  for (const [key, label] of heroFields) {
    const wrap = doc.createElement('div'); wrap.className = 'metric';
    const caption = doc.createElement('label'); caption.textContent = label;
    const value = doc.createElement('b'); value.textContent = '--'; value.dataset.field = key;
    wrap.append(caption, value); get('hero').append(wrap); hero.set(key, value);
  }
  function update(cells, before, after, animate) {
    const delta = rowDelta(before, after);
    for (const key of delta.fields) {
      const cell = cells.get(key); if (!cell) continue;
      text(cell, display(after[key], key));
      if (['change','change_pct','speed_1m','speed_3m','speed_5m'].includes(key)) {
        const color = numeric(after[key]) ? (after[key] > 0 ? 'up' : after[key] < 0 ? 'down' : '') : '';
        if (cell.className !== color) cell.className = color;
      }
    }
    if (animate && delta.flash) {
      const cell = cells.get('lastPrice');
      cell.getAnimations().forEach(animation => animation.cancel());
      const color = delta.flash === 'up' ? '#d92d20' : '#079455';
      const backgroundColor = delta.flash === 'up' ? '#fee4e2' : '#d1fadf';
      cell.animate([{color, backgroundColor}, {color:'inherit', backgroundColor:'transparent'}], {duration:400});
    }
  }
  return {
    apply(payload) {
      const started = performance.now();
      const status = payload.status;
      text(get('market'), '市场：' + status.market);
      text(get('quote-status'), status.quote_status);
      text(get('market-time'), display(status.last_quote_time, 'quote_time'));
      // Status events are independent of sequence; duplicate snapshots never animate.
      if (payload.snapshot_seq > seq) {
        const animate = seq >= 0 && payload.from_cache === false && status.market_session === 'open' && status.quote_status === 'LIVE';
        if (payload.target) {
          update(hero, target, payload.target, animate);
          text(get('symbol-name'), payload.target.name || '');
          target = {...payload.target};
        } else {
          for (const cell of hero.values()) { text(cell, '--'); cell.className = ''; }
          text(get('symbol-name'), ''); target = null;
        }
        const keep = new Set(payload.top20.map(row => row.symbol));
        for (const [symbol, entry] of rows) if (!keep.has(symbol)) { entry.node.remove(); rows.delete(symbol); }
        payload.top20.forEach((row, index) => {
          let entry = rows.get(row.symbol);
          if (!entry) {
            const node = doc.createElement('div'); node.className = 'rank-row'; node.dataset.symbol = row.symbol;
            const cells = new Map();
            for (const key of rankFields) { const cell = doc.createElement('span'); node.append(cell); cells.set(key, cell); }
            get('ranking').append(node); entry = {node, cells, data:null}; rows.set(row.symbol, entry);
          }
          text(entry.cells.get('rank'), String(index + 1));
          const position = 'translateY(' + index * 36 + 'px)';
          entry.node.style.transition = animate ? '' : 'none';
          if (entry.node.style.transform !== position) entry.node.style.transform = position;
          update(entry.cells, entry.data, row, animate); entry.data = {...row};
        });
        const height = payload.top20.length * 36 + 'px';
        if (get('ranking').style.height !== height) get('ranking').style.height = height;
        get('empty').hidden = payload.top20.length > 0;
        seq = payload.snapshot_seq;
      }
      lastRenderMs = performance.now() - started;
      updates++;
      text(get('diagnostics'), (acceptanceMode ? '共享快照 ' + payload.snapshot_seq + '｜有效行情 ' + payload.valid_quotes +
        '｜Provider 初始化 ' + payload.provider_init_count + '｜请求 ' + payload.provider_request_count :
        (status.quote_status === 'CACHED' ? '显示最近有效行情' : '行情随市场更新')) +
        (status.quote_status === 'CACHED' ? '｜1m/3m/5m：' + (status.market_session === 'closed' ? '收盘前最后值' : '休市前最后值') + '；无可靠历史显示 --' : '') +
        (payload.error ? '｜取数失败：' + payload.error : ''));
      get('diagnostics').dataset.renderMs = lastRenderMs.toFixed(2);
      get('diagnostics').dataset.updates = String(updates);
    }
  };
}

export class LatencySamples {
  constructor(limit=300) { this.limit=limit; this.samples=[]; }
  add(payload, received, applied, uiTime, uiStatus) {
    const item = {event_id:payload.event_id, snapshot_seq:payload.snapshot_seq,
      ...payload.trace, feed_published_at:payload.feed_published_at, sse_sent_at:payload.sse_sent_at,
      feed_to_sse_ms:payload.feed_to_sse_ms, client_received_at:performance.timeOrigin+received,
      dom_applied_at:performance.timeOrigin+applied, receive_to_dom_ms:applied-received,
      ui_quote_time:uiTime, ui_quote_status:uiStatus};
    this.samples.push(item);
    if(this.samples.length>this.limit) this.samples.splice(0,this.samples.length-this.limit);
  }
}

export function connectStream(url, renderer, connection, EventSourceClass, button, onApplied=null) {
  let source = null;
  const set = value => { if (connection.textContent !== value) connection.textContent = value; };
  function connect() {
    if (source) source.close();
    set('连接中');
    source = new EventSourceClass(url);
    source.onopen = () => set('已连接 · SSE');
    source.onerror = () => set('连接中断 · 自动重连中');
    source.onmessage = event => {
      const received = performance.now();
      try {
        const payload=JSON.parse(event.data);
        renderer.apply(payload);
        const applied=performance.now();
        set('已连接 · SSE');
        if(onApplied) onApplied(payload,received,applied);
      }
      catch { set('数据不可用 · 等待下一快照'); }
    };
  }
  button.onclick = connect;
  connect();
  return () => { if (source) source.close(); source = null; };
}

if (typeof document !== 'undefined') {
  const renderer = createRenderer(document, {acceptanceMode:new URLSearchParams(location.search).get('acceptance') === '1'});
  const timings = new LatencySamples();
  const endpoint = new URL('events', location.href); endpoint.search = location.search;
  const stop = connectStream(endpoint.href, renderer, document.getElementById('connection'), EventSource,
                             document.getElementById('reconnect'), (payload,received,applied)=>{
    if(!payload.diagnostics_enabled) return;
    timings.add(payload,received,applied,document.getElementById('market-time').textContent,
                document.getElementById('quote-status').textContent);
    document.getElementById('diagnostics').dataset.trace=JSON.stringify(timings.samples);
  });
  window.addEventListener('pagehide', stop, {once:true});
}
