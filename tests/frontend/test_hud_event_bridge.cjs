/* Run with: node --test tests/frontend/test_hud_event_bridge.cjs */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const hud = fs.readFileSync(path.join(root,'jarvis/ui/web/frontend/public/jarvis-hud.js'),'utf8');
const runtime = fs.readFileSync(path.join(root,'jarvis/ui/web/frontend/public/jarvis-hud-bridge.js'),'utf8');
const matcher = fs.readFileSync(path.join(root,'jarvis/ui/web/frontend/public/jarvis-hud-intents.js'),'utf8');
const proto = 'jarvis:hud:user-intent:v1';
function makeRuntime(opts={}) {
  const messages=[];const sockets=[];const fetches=[];
  const parent={location:{origin:opts.parentOrigin||'https://jarvis.local'},postMessage:(data,origin)=>messages.push({data,origin})};
  const location={search:opts.search||'?solo=1&jarvis_hud=off',origin:'https://jarvis.local',href:'https://jarvis.local/?solo=1',host:'jarvis.local',protocol:'https:'};
  const fetch=opts.fetch||((_input,_init)=>Promise.resolve({ok:true}));
  class Socket {
    constructor(url){this.url=url;this.listeners={};sockets.push(this)}
    addEventListener(type,fn){(this.listeners[type]??=[]).push(fn)}
    emit(event){for(const fn of this.listeners.message||[])fn({data:JSON.stringify(event)})}
  }
  const window={parent,location,fetch:function(...args){fetches.push(args);return fetch(...args)},WebSocket:Socket};
  vm.runInNewContext(runtime,{window,location,URL,URLSearchParams,Reflect,Promise,JSON,String});
  return {window,parent,location,messages,sockets,fetches};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
// Function: actual runtime request and WebSocket event behavior.
test('function: successful accepted typed message emits one display intent',async()=>{
 const x=makeRuntime();const r=await x.window.fetch('/api/agent-chat/sessions/s1/messages',{method:'POST',body:JSON.stringify({text:'Jarvis, zeig meine Aufgaben'})});await settle();
 assert.equal(r.ok,true);assert.equal(x.messages.length,1);assert.deepEqual(JSON.parse(JSON.stringify(x.messages[0])),{data:{type:proto,text:'Jarvis, zeig meine Aufgaben'},origin:'https://jarvis.local'});
});
test('function: rejected send never emits',async()=>{const x=makeRuntime({fetch:()=>Promise.resolve({ok:false})});await x.window.fetch('/api/agent-chat/sessions/s1/messages',{method:'POST',body:'{"text":"Öffne Gedächtnis"}'});await settle();assert.equal(x.messages.length,0)});
test('function: final voice event emits; partial and assistant events ignored',()=>{
 const x=makeRuntime();const ws=new x.window.WebSocket('wss://jarvis.local/ws?ticket=secret');
 ws.emit({event_name:'TranscriptionUpdate',payload:{is_final:false,text:'Öffne'}});
 ws.emit({event_name:'MessageSent',payload:{role:'assistant',text:'Öffne System'}});
 ws.emit({event_name:'TranscriptFinal',payload:{transcript:{text:'Öffne Systemstatus'}}});
 assert.equal(x.messages.length,1);assert.equal(x.messages[0].data.text,'Öffne Systemstatus');
});
test('function: final transcription and typed legacy event are accepted',()=>{
 const x=makeRuntime();const ws=new x.window.WebSocket('wss://jarvis.local/ws');
 ws.emit({event_name:'TranscriptionUpdate',payload:{is_final:true,text:'Zeig mir RAM'}});
 ws.emit({event_name:'MessageSent',payload:{role:'user',text:'Öffne Chat'}});
 assert.deepEqual(x.messages.map(m=>m.data.text),['Zeig mir RAM','Öffne Chat']);
});
// Integration: validate actual parent handler with actual intent matcher.
function makeParent(){
 const matcherWindow={};vm.runInNewContext(matcher,{window:matcherWindow});
 const popups=[];const notices=[];const frameWindow={};const frame={contentWindow:frameWindow};
 const context={window:{JarvisHudIntent:matcherWindow.JarvisHudIntent},location:{origin:'https://jarvis.local'},panel:{},coreOnly:true,intentFrame:frame,lastIntent:null,Date,openCorePopup:(kind,source)=>{popups.push({kind,source})},notice:s=>notices.push(s)};
 const source=hud.slice(hud.indexOf('  function onUserUtterance(text) {'),hud.indexOf('  function showTab(tab) {'));
 assert.ok(source.includes('function onHudMessage(event)'));
 vm.runInNewContext(source+';globalThis.route = onHudMessage;',context);
 return {route:context.route,popups,notices,frameWindow,context};
}
test('integration: verified iframe opens tasks from final user command',()=>{
 const p=makeParent();p.route({origin:'https://jarvis.local',source:p.frameWindow,data:{type:proto,text:'Jarvis, zeig mir meine Aufgaben'}});
 assert.deepEqual(p.popups,[{kind:'tasks',source:'auto'}]);
});
test('integration: duplicate final STT and typed events open only once',()=>{
 const p=makeParent();const ev={origin:'https://jarvis.local',source:p.frameWindow,data:{type:proto,text:'Öffne das Gedächtnis'}};
 p.route(ev);p.route(ev);assert.deepEqual(p.popups,[{kind:'memory',source:'auto'}]);
});
test('integration: actual runtime event flows to parent handler',async()=>{
 const x=makeRuntime();const p=makeParent();x.parent.postMessage=(data,origin)=>p.route({origin,source:p.frameWindow,data});
 await x.window.fetch('/api/agent-chat/sessions/s1/messages',{method:'POST',body:JSON.stringify({text:'Zeig mir den Systemstatus'})});await settle();
 assert.deepEqual(p.popups,[{kind:'system',source:'auto'}]);
});
// Security/regression: strict source/origin, no private info or untrusted text.
test('security: forged origin and wrong iframe source are rejected',()=>{
 const p=makeParent();const data={type:proto,text:'Öffne Chat'};
 p.route({origin:'https://evil.example',source:p.frameWindow,data});
 p.route({origin:'https://jarvis.local',source:{},data});
 p.route({origin:'https://jarvis.local',source:p.frameWindow,data:{type:'other',text:'Öffne Chat'}});
 assert.equal(p.popups.length,0);
});
test('security: malformed payload and overly long text rejected',()=>{
 const p=makeParent();for(const data of [null,'Öffne Chat',{type:proto,text:12},{type:proto,text:'x'.repeat(801)}])p.route({origin:'https://jarvis.local',source:p.frameWindow,data});
 assert.equal(p.popups.length,0);
});
test('security: unrelated requests, sockets and non-final events never forward',async()=>{
 const x=makeRuntime();await x.window.fetch('https://evil.example/api/agent-chat/sessions/x/messages',{method:'POST',body:'{"text":"Öffne Chat"}'});
 await x.window.fetch('/api/agent-chat/sessions/s1/messages',{method:'GET',body:'{"text":"Öffne Chat"}'});
 const ws=new x.window.WebSocket('wss://evil.example/ws');ws.emit({event_name:'TranscriptFinal',payload:{transcript:{text:'Öffne Chat'}}});
 const mission=new x.window.WebSocket('wss://jarvis.local/api/missions/ws');mission.emit({event_name:'TranscriptFinal',payload:{transcript:{text:'Öffne Chat'}}});
 await settle();assert.equal(x.messages.length,0);
});
test('security: no bridge in top-level, unrelated or cross-origin frames',async()=>{
 const x=makeRuntime({search:'?view=chats'});await x.window.fetch('/api/agent-chat/sessions/s1/messages',{method:'POST',body:'{"text":"Öffne Chat"}'});await settle();assert.equal(x.messages.length,0);
 const y=makeRuntime({parentOrigin:'https://evil.example'});await y.window.fetch('/api/agent-chat/sessions/s1/messages',{method:'POST',body:'{"text":"Öffne Chat"}'});await settle();assert.equal(y.messages.length,0);
});
test('regression: no DOM scraping and no chat iframe reparenting in bridge',()=>{
 assert.ok(!hud.includes('MutationObserver'));assert.ok(!hud.includes('querySelectorAll(\'.jarvis-user-bubble'));
 assert.ok(!runtime.includes('querySelector'));assert.ok(!runtime.includes('localStorage'));assert.ok(!runtime.includes('document.cookie'));
 assert.ok(hud.includes('CSS positions the existing iframe'));
});
// Source-level React emitter: transpile the exact TS module and exercise it.
test('function: React bridge only posts from an authorized same-origin solo iframe',()=>{
 const ts=require('/opt/nvm/versions/node/v22.16.0/lib/node_modules/typescript');
 const source=fs.readFileSync(path.join(root,'jarvis/ui/web/frontend/src/lib/hudEventBridge.ts'),'utf8');
 const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 const sent=[];const parent={location:{origin:'https://jarvis.local'},postMessage:(data,origin)=>sent.push({data,origin})};
 const window={parent,location:{search:'?solo=1&jarvis_hud=off',origin:'https://jarvis.local'}};
 const exports={};vm.runInNewContext(compiled,{exports,window,URLSearchParams});
 exports.emitHudUserIntent('Öffne Chat');exports.emitHudUserIntent('x'.repeat(900));exports.emitHudUserIntent({text:'Öffne Chat'});
 assert.equal(sent.length,1);assert.equal(sent[0].data.text,'Öffne Chat');
 window.location.search='?view=chats';exports.emitHudUserIntent('Öffne Chat');assert.equal(sent.length,1);
 parent.location.origin='https://evil.example';window.location.search='?solo=1&jarvis_hud=off';exports.emitHudUserIntent('Öffne Chat');assert.equal(sent.length,1);
});
test('regression: shipped dist and source HUD bridge match',()=>{
 const source=fs.readFileSync(path.join(root,'jarvis/ui/web/frontend/public/jarvis-hud.js'),'utf8');
 const dist=fs.readFileSync(path.join(root,'jarvis/ui/web/dist/jarvis-hud.js'),'utf8');
 assert.equal(source,dist);
 for(const html of ['jarvis/ui/web/dist/index.html','jarvis/ui/web/frontend/index.html']){
  const s=fs.readFileSync(path.join(root,html),'utf8');assert.ok(s.includes('/jarvis-hud-bridge.js'));
 }
});
