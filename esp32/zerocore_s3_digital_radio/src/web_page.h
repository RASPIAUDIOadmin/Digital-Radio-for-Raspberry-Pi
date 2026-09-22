#pragma once

// Served entirely from the ESP32-S3; no CDN or Internet connection is needed.
static const char RADIO_WEB_PAGE[] PROGMEM = R"RADIO_HTML(<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#111827">
  <title>RASPIAUDIO Digital Radio</title>
  <style>
    :root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#edf3f8;background:#0b111b}
    *{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;min-height:100vh;background:radial-gradient(circle at 75% 0%,#19374b 0,#0b111b 50%)}
    main{max-width:950px;margin:auto;padding:24px 16px 56px}header{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:25px}
    .brand{font-size:13px;letter-spacing:.2em;font-weight:800;color:#55dfc7}.tag{font-size:12px;color:#a3b4c4;border:1px solid #355063;border-radius:30px;padding:8px 12px}
    h1{font-size:clamp(30px,7vw,58px);line-height:1.02;letter-spacing:-.04em;margin:0 0 12px}h2{font-size:20px;margin:0 0 15px}
    p{color:#a9bac9;line-height:1.45}.hero{padding:28px;background:linear-gradient(125deg,#15374a,#10202f 63%,#152133);border:1px solid #2b5364;border-radius:26px;margin-bottom:16px}
    .eyebrow{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:#70e8d5;font-weight:800;margin:0 0 10px}
    .now{font-size:25px;font-weight:750;margin:8px 0}.meta{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}.pill{padding:7px 12px;border-radius:30px;background:#ffffff17;color:#d3e4ed;font-size:13px}
    .grid{display:grid;grid-template-columns:1.35fr 1fr;gap:16px}.card{background:#111e2c;border:1px solid #304153;border-radius:22px;padding:22px;margin-bottom:16px}
    .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.row>*{min-width:0}button,input,select{font:inherit}button{border:0;border-radius:12px;padding:11px 15px;background:#263a4c;color:#f5faff;font-weight:700;cursor:pointer}
    button:hover{background:#345369}button.primary{background:#5ce7cd;color:#062b31}button.primary:hover{background:#8df6e2}button.selected{outline:2px solid #5ce7cd}
    button:disabled{opacity:.5;cursor:wait}input[type=number],input[type=text],input[type=password],select{background:#0b1723;color:#fff;border:1px solid #436072;border-radius:12px;padding:10px 12px;min-height:42px}
    input[type=text],input[type=password]{width:100%}.field{display:grid;gap:6px;margin-top:12px;color:#c9d9e5;font-size:13px}.wifi-actions{margin-top:14px}
    input[type=number]{width:125px}input[type=range]{flex:1;accent-color:#5ce7cd;min-width:100px}.scanline{font-size:13px;color:#a9bac9;min-height:20px}
    .station-list{display:grid;gap:8px;max-height:390px;overflow:auto}.station{display:flex;justify-content:space-between;align-items:center;gap:12px;background:#182b39;border:1px solid #304c5b;border-radius:13px;padding:10px 12px}
    .station strong{display:block}.station small{display:block;color:#9fb5c2;margin-top:4px}.station button{flex:none;padding:8px 12px}
    .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.stat{border:1px solid #314757;border-radius:13px;padding:12px;background:#162938}.stat b{display:block;font-size:20px;margin-top:4px}.stat small{color:#a8bdca}
    .notice{font-size:13px;min-height:22px;margin:0;color:#91ead9}.error{color:#ffacaa}.hint{font-size:12px;margin:15px 0 0}
    @media(max-width:720px){.grid{grid-template-columns:1fr}header{align-items:start}.hero{padding:23px}.card{padding:18px}}
  </style>
</head>
<body>
<main>
  <header><div class="brand">RASPIAUDIO</div><div class="tag">LOCAL RADIO · ESP32-S3</div></header>
  <section class="hero">
    <p class="eyebrow">Now playing</p><h1>Digital Radio</h1>
    <div id="now" class="now">Waiting for radio…</div>
    <p id="sub">Audio plays through the shield's jack and speaker amplifier.</p>
    <div class="meta"><span id="modePill" class="pill">—</span><span id="signalPill" class="pill">Signal —</span><span id="ampPill" class="pill">Amplifier —</span></div>
  </section>
  <div class="grid">
    <div>
      <section class="card"><h2>Source</h2><div class="row"><button id="fmMode" type="button">FM</button><button id="dabMode" type="button">DAB+</button></div>
        <p class="hint">Changing source reloads the SI4689 firmware and turns off the amplifier.</p></section>
      <section class="card"><h2>Tune and scan</h2>
        <div id="fmTune" class="row"><input id="fmFrequency" type="number" min="87.5" max="108" step="0.01" value="101.10" aria-label="FM frequency in MHz"><span>MHz</span><button id="fmGo" class="primary" type="button">Tune</button></div>
        <div id="dabTune" class="row" hidden><select id="dabChannel" aria-label="DAB channel"></select><button id="dabGo" class="primary" type="button">Tune</button><button id="loadServices" type="button">Load stations</button></div>
        <div class="row" style="margin-top:14px"><button id="scan" type="button">Scan band</button><span id="scanText" class="scanline"></span></div>
      </section>
      <section class="card"><h2>Stations</h2><div id="stations" class="station-list"><p>No stations loaded.</p></div></section>
    </div>
    <div>
      <section class="card"><h2>Audio output</h2><div class="row"><label for="volume">Volume <strong id="volValue">40</strong>/63</label><input id="volume" type="range" min="0" max="63" value="40"></div>
        <div class="row" style="margin-top:16px"><button id="amp" type="button">Turn on amplifier</button></div>
        <p class="hint">The analog jack works without turning on the speaker amplifier.</p></section>
      <section class="card"><h2>Reception</h2><div class="stats"><div class="stat"><small>RSSI</small><b id="rssi">—</b></div><div class="stat"><small>SNR</small><b id="snr">—</b></div><div class="stat"><small id="qualityLabel">FIC quality</small><b id="quality">—</b></div></div>
        <p id="statusText" class="hint">Waiting for status…</p></section>
      <section class="card"><h2>Connection</h2><p id="wifiState">Checking network…</p><p class="hint">Local address: <strong id="wifiAddress">—</strong></p>
        <div id="wifiConfig" hidden><p class="hint">From the hotspot, configure a local Wi-Fi network. If the connection fails, the hotspot returns automatically.</p>
          <label class="field" for="wifiSsid">Network name (SSID)<input id="wifiSsid" type="text" maxlength="32" autocomplete="off"></label>
          <label class="field" for="wifiPassword">Password<input id="wifiPassword" type="password" minlength="8" maxlength="63" autocomplete="new-password"></label>
          <div class="row wifi-actions"><button id="wifiSave" class="primary" type="button">Connect</button><button id="wifiRetry" type="button" hidden>Retry</button><button id="wifiClear" type="button">Forget network</button></div>
        </div></section>
    </div>
  </div>
  <p id="message" class="notice" role="status" aria-live="polite"></p>
</main>
<script>
const $=id=>document.getElementById(id);
let state=null,working=false;
function message(text,bad=false){$('message').textContent=text;$('message').className=bad?'notice error':'notice'}
async function request(path,fields){const options=fields?{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(fields)}:{};const response=await fetch(path,options);const data=await response.json();if(!response.ok||data.ok===false)throw Error(data.error||'Command failed');return data}
async function action(path,fields,text){if(working)return;working=true;message(text||'Command in progress…');try{await request(path,fields);await refresh();message('Command completed.')}catch(e){message(e.message,true)}finally{working=false}}
function stationButton(label,detail,callback){const row=document.createElement('div');row.className='station';const info=document.createElement('div');const title=document.createElement('strong');title.textContent=label;const small=document.createElement('small');small.textContent=detail;info.append(title,small);const button=document.createElement('button');button.type='button';button.textContent='Play';button.onclick=callback;row.append(info,button);return row}
function render(data){state=data;const dab=data.mode==='dab';$('fmMode').classList.toggle('selected',!dab);$('dabMode').classList.toggle('selected',dab);$('fmTune').hidden=dab;$('dabTune').hidden=!dab;
  $('modePill').textContent=dab?'DAB+':'FM';$('signalPill').textContent=data.valid?'Signal detected':'Searching for signal';$('ampPill').textContent=data.amp?'Amplifier on':'Amplifier off';
  $('now').textContent=data.station||(!dab&&data.frequency?Number(data.frequency).toFixed(2)+' MHz':dab&&data.channel?'Channel '+data.channel:'No station');
  $('sub').textContent=dab?(data.channel?'Multiplex '+data.channel:'Choose a DAB channel'):(data.frequency?'Frequency '+Number(data.frequency).toFixed(2)+' MHz':'Choose an FM frequency');
  $('volValue').textContent=data.volume;if(document.activeElement!==$('volume'))$('volume').value=data.volume;
  $('amp').textContent=data.amp?'Turn off amplifier':'Turn on amplifier';$('rssi').textContent=data.rssi??'—';$('snr').textContent=data.snr??'—';
  $('qualityLabel').textContent=dab?'FIC quality':'Signal';$('quality').textContent=dab?(data.fic_quality??'—'):(data.valid?'Valid':'—');
  $('statusText').textContent=data.ready?(data.valid?'Valid reception':'Radio ready, no signal lock'):'Radio unavailable';
  const wifi=data.wifi||{};$('wifiState').textContent=wifi.mode==='sta'?'Local network: '+wifi.ssid:wifi.mode==='ap'?'Hotspot: '+wifi.ssid+(wifi.configured_ssid?' · connection to '+wifi.configured_ssid+' failed':''):'Connecting to Wi-Fi…';
  $('wifiAddress').textContent=wifi.ip?'http://'+wifi.ip+'/':'—';$('wifiConfig').hidden=wifi.mode!=='ap';
  $('wifiRetry').hidden=!wifi.configured_ssid;
  const scan=data.scan||{};$('scanText').textContent=scan.active?`Progress ${scan.current}/${scan.total} · ${scan.found} found`:scan.total?`${scan.found} found in the last scan`:'';
  $('scan').disabled=!!scan.active;
  const list=$('stations');list.replaceChildren();const stations=dab?data.services:data.fm_stations;
  if(dab&&data.multiplexes&&data.multiplexes.length){const p=document.createElement('p');p.textContent='Detected multiplexes';list.append(p);for(const mux of data.multiplexes)list.append(stationButton('Channel '+mux.channel,`RSSI ${mux.rssi} · FIC ${mux.fic_quality}`,()=>{ $('dabChannel').value=mux.channel;action('/api/tune',{channel:mux.channel},'Tuning DAB…')}))}
  if(!stations||!stations.length){const p=document.createElement('p');p.textContent=dab?'Tune a channel, then load stations.':'Start a scan or enter a frequency.';list.append(p)}
  else for(const item of stations){if(dab)list.append(stationButton(item.label,`Service ${item.index}`,()=>action('/api/play',{index:item.index},'Starting DAB playback…')));
    else list.append(stationButton(Number(item.frequency).toFixed(2)+' MHz',`RSSI ${item.rssi} · SNR ${item.snr}`,()=>action('/api/tune',{frequency:item.frequency},'Tuning FM…')))}
}
async function refresh(){const data=await request('/api/status');render(data)}
$('fmMode').onclick=()=>action('/api/mode',{mode:'fm'},'Loading FM…');$('dabMode').onclick=()=>action('/api/mode',{mode:'dab'},'Loading DAB…');
$('fmGo').onclick=()=>action('/api/tune',{frequency:$('fmFrequency').value},'Tuning FM…');
$('dabGo').onclick=()=>action('/api/tune',{channel:$('dabChannel').value},'Tuning DAB…');
$('loadServices').onclick=()=>action('/api/services',{},'Loading station list…');
$('scan').onclick=()=>action('/api/scan',{},'Scan started…');
$('amp').onclick=()=>action('/api/amp',{on:state&&state.amp?'0':'1'},'Setting amplifier…');
$('volume').oninput=()=>{$('volValue').textContent=$('volume').value};
$('volume').onchange=()=>action('/api/volume',{value:$('volume').value},'Setting volume…');
$('wifiSave').onclick=async()=>{const ssid=$('wifiSsid').value.trim(),password=$('wifiPassword').value;if(!ssid||password.length<8){message('An SSID and a password of at least 8 characters are required.',true);return}if(working)return;working=true;message('Credentials saved. Connecting; the hotspot will return if the connection fails.');try{await request('/api/wifi',{ssid,password});$('wifiPassword').value='';message('Credentials saved. Join the local network; the hotspot will return if the connection fails.')}catch(e){message(e.message,true)}finally{working=false}};
$('wifiClear').onclick=()=>action('/api/wifi/clear',{},'Forgetting saved network…');
$('wifiRetry').onclick=async()=>{if(working)return;working=true;try{await request('/api/wifi/retry',{});message('Retrying the saved network. The hotspot will return if the connection fails.')}catch(e){message(e.message,true)}finally{working=false}};
async function init(){try{const channels=await request('/api/channels');for(const name of channels.channels){const option=document.createElement('option');option.value=name;option.textContent=name;$('dabChannel').append(option)}$('dabChannel').value='11B';await refresh()}catch(e){message('Cannot connect to the radio: '+e.message,true)}}
setInterval(()=>{if(!working)refresh().catch(e=>message('Connection lost: '+e.message,true))},2000);init();
</script>
</body>
</html>)RADIO_HTML";
