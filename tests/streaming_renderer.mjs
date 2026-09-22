import assert from 'node:assert/strict';
import fs from 'node:fs';
const code = fs.readFileSync(new URL('../ui/streaming/stream.js', import.meta.url), 'utf8');
const {priceDirection, rowDelta, createRenderer, connectStream, LatencySamples} = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
assert.equal(priceDirection(10,10,1,2),'');
assert.equal(priceDirection(10,11,1,2),'up');
assert.equal(priceDirection(10,9,1,2),'down');
assert.equal(priceDirection(10,11,2,2),'');
assert.equal(priceDirection(10,10,2,3),'');
assert.deepEqual(rowDelta({snapshot_seq:2,lastPrice:10},{snapshot_seq:2,lastPrice:11}).fields,[]);

// DOM double tests the exported production renderer, not a separate animation implementation.
class Element {
  constructor(){this.children=[];this.dataset={};this.style={};this.className='';this.animations=[];this.writes=0;this.value='';}
  set textContent(value){this.writes++;this.value=value;}
  get textContent(){return this.value;}
  append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node);}}
  remove(){this.parent.children=this.parent.children.filter(node=>node!==this);}
  getAnimations(){return [];}
  animate(frames,options){this.animations.push({frames,options});}
}
const ids = new Map();
const doc = {createElement:()=>new Element(),getElementById(id){if(!ids.has(id))ids.set(id,new Element());return ids.get(id);}};
const renderer = createRenderer(doc);
const row = (symbol,seq,price) => ({symbol,snapshot_seq:seq,lastPrice:price,change_pct:price-10,name:symbol});
const packet = (seq,price,order=['A','B'],status='LIVE') => ({snapshot_seq:seq,from_cache:false,
  status:{market:'交易中',market_session:'open',quote_status:status,last_quote_time:'2026-09-16T10:00:00'},
  target:row('600498.SH',seq,price),top20:order.map(s=>row(s,seq,price)),provider_init_count:1,provider_request_count:seq,valid_quotes:2});
renderer.apply(packet(1,10));
const heroPrice=ids.get('hero').children[0].children[1];
const firstRow=ids.get('ranking').children[0];
renderer.apply(packet(2,11,['B','A']));
assert.equal(ids.get('ranking').children[0],firstRow); // key retained, not replaced
assert.equal(firstRow.style.transform,'translateY(36px)');
assert.equal(firstRow.children[0].textContent,'2');
assert.equal(heroPrice.animations.length,1);
assert.equal(heroPrice.animations[0].frames[0].color,'#d92d20');
assert.equal(heroPrice.animations[0].options.duration,400);
const writes=heroPrice.writes;
renderer.apply(packet(2,11,['B','A'],'STALE'));
assert.equal(ids.get('quote-status').textContent,'STALE');
assert.equal(heroPrice.animations.length,1);
assert.equal(heroPrice.writes,writes);
renderer.apply(packet(3,11,['B','A'],'CACHED'));
assert.equal(heroPrice.animations.length,1);
assert.equal(ids.get('quote-status').textContent,'CACHED');
renderer.apply(packet(4,9));
assert.equal(heroPrice.animations[1].frames[0].color,'#079455');
const cached=packet(5,12);cached.from_cache=true;renderer.apply(cached);
assert.equal(heroPrice.animations.length,2); // cached replay never flashes
const closed=packet(6,13,['A','B'],'CACHED');closed.status.market_session='closed';renderer.apply(closed);
assert.equal(heroPrice.textContent,'13.00');
assert.equal(heroPrice.animations.length,2); // even fresh one-shot results cannot flash after close
assert.equal(firstRow.style.transition,'none');
assert.doesNotMatch(ids.get('diagnostics').textContent,/Provider|请求|共享快照/);
const developerIds=new Map();
const developerDoc={createElement:()=>new Element(),getElementById(id){if(!developerIds.has(id))developerIds.set(id,new Element());return developerIds.get(id);}};
createRenderer(developerDoc,{acceptanceMode:true}).apply(closed);
assert.match(developerIds.get('diagnostics').textContent,/共享快照/);

class Source {
  static all=[];
  constructor(url){this.url=url;this.closed=false;Source.all.push(this);}
  close(){this.closed=true;}
}
const connection=new Element(),button={};
const stop=connectStream('/events',renderer,connection,Source,button);
Source.all[0].onopen();assert.match(connection.textContent,/已连接/);
Source.all[0].onerror();assert.match(connection.textContent,/自动重连/);
button.onclick();assert.equal(Source.all[0].closed,true);assert.equal(Source.all.length,2);
Source.all[1].onmessage({data:JSON.stringify(cached)});
assert.equal(heroPrice.animations.length,2);
stop();assert.equal(Source.all[1].closed,true);
const latency = new LatencySamples(3);
for(let i=0;i<10;i++)latency.add({snapshot_seq:i,feed_to_sse_ms:2},100,104,'time','LIVE');
assert.equal(latency.samples.length,3);
assert.equal(latency.samples[0].snapshot_seq,7);
assert.equal(latency.samples[2].receive_to_dom_ms,4);
assert.equal(latency.samples[2].dom_applied_at-latency.samples[2].client_received_at,4);
assert.equal(latency.samples[2].feed_to_dom_ms,undefined); // no cross-clock subtraction
console.log('incremental DOM, colors, deduplication, rank, status, reconnect: PASS');
